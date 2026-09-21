"""OpenSwap account engine: AccountsSnapshot assembler. Store-only paint does not read idle backup credentials."""

from __future__ import annotations

from openswap.engine.notes import *  # noqa: F403

class SnapshotMixin:
    """AccountsSnapshot assembler. Store-only paint does not read idle backup credentials."""

    def usage_by_account(self) -> dict[str, dict | str | None]:
        """Public wrapper: account number → decision-grade usage value.

        Each value is a usage dict (last-good, trusted while ≤
        ``usage_store.STALE_OK_S`` old), a sentinel string, or ``None``
        (unknown).
        """
        return self._usage_by_account()

    def usage_entries_by_account(
        self, fetch: set[str] | None = None, *, scheduled: bool = False
    ) -> dict[str, UsageEntry]:
        """Store-backed usage entries (ages, errors, poll state) per account.

        ``fetch`` restricts which accounts *may* be fetched this pass (the
        auto engine's scheduler); ``None`` means every stale account is
        eligible (on-demand callers). ``scheduled=True`` preserves valid
        future plans while still allowing due plans to beat the serve TTL.
        """
        accounts_info = self._build_accounts_info(load_idle=fetch if fetch is not None else True)
        return self._collect_usage_entries(
            accounts_info, fetch=fetch, scheduled=scheduled
        )

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        """One-pass structured snapshot of every managed account, for GUIs.

        Metadata, active-slot detection, and usage entries all come from a
        single ``_build_accounts_info`` + ``_collect_usage_entries`` pass, so
        the view is coherent — two separate calls could interleave with other
        collectors and disagree about the active slot or freshness. ``fetch``
        has ``_collect_usage_entries`` semantics: ``None`` makes every stale
        account eligible; a set restricts which accounts *may* be fetched
        this pass.

        Store-only paint (``fetch=set()``) is roster-only: neither idle
        nor active-slot backup credentials are read. Unread idle slots
        are not classified ``USAGE_NO_CREDENTIALS``.
        """
        load_idle: bool | set[str] = True if fetch is None else fetch
        accounts_info = self._build_accounts_info(load_idle=load_idle)
        entries = self._collect_usage_entries(accounts_info, fetch=fetch)
        seq_data = self._get_sequence_data() or {}
        active_number: str | None = None
        accounts: list[AccountSnapshot] = []
        store_only = fetch is not None and not fetch
        for num, email, org_name, org_uuid, is_active, creds, alias in accounts_info:
            n = str(num)
            if is_active:
                active_number = n
            if creds is UNREAD_CREDENTIALS or store_only:
                switchable = self._roster_has_config(n)
            else:
                switchable = self._account_is_switchable(n)
            accounts.append(
                AccountSnapshot(
                    number=n,
                    email=email,
                    org_name=org_name,
                    org_uuid=org_uuid,
                    is_active=is_active,
                    kind=self._account_kind(n),
                    switchable=switchable,
                    usage=entries[n],
                    alias=alias,
                    disabled=self._disabled_from_data(seq_data, n),
                )
            )
        return AccountsSnapshot(
            active_number=active_number,
            accounts=tuple(accounts),
            taken_at=self._usage_store.clock(),
        )

    def usage_fetch_stamps(self) -> dict[str, float | None]:
        """Per-slot ``fetchedAt`` snapshot from the usage store — a pure file
        read (no fetching, no credential access). Dashboards can diff
        consecutive snapshots to flash rows whose usage just refreshed.
        """
        data = self._get_sequence_data() or {}
        identities = {
            num: (info.get("email", ""), info.get("organizationUuid", "") or "")
            for num, info in data.get("accounts", {}).items()
        }
        # No models needed: only fetched_at is read, never scoped-window trust.
        return {
            num: entry.fetched_at
            for num, entry in self._usage_store.entries(identities).items()
        }

    def set_poll_policy_inputs(
        self, threshold: float, models: tuple[str, ...]
    ) -> None:
        """Pin the threshold/models poll planning keys on (set by a hosted
        auto engine so cadence follows its effective, CLI-merged settings
        instead of the settings file)."""
        self._poll_inputs_override = (threshold, models)

    def clear_poll_policy_inputs(self) -> None:
        """Drop the hosted engine's pin so poll planning falls back to the
        settings file — called when a hosted engine stops, so a session
        threshold override does not keep steering cadence after it is gone."""
        self._poll_inputs_override = None

    def _poll_policy_inputs(self) -> tuple[float, tuple[str, ...]]:
        """Threshold + configured model names for poll planning: the hosting
        engine's pinned values when present, else the settings file (reloaded
        only when it changes — one stat per pass)."""
        if self._poll_inputs_override is not None:
            return self._poll_inputs_override
        path = settings_path(self.backup_dir)
        try:
            mtime: float | None = path.stat().st_mtime
        except OSError:
            mtime = None
        if self._poll_inputs_cache is not None and self._poll_inputs_cache[0] == mtime:
            return self._poll_inputs_cache[1]
        loaded = load_settings(self.backup_dir)
        inputs = (loaded.threshold, parse_model_names(loaded.model))
        self._poll_inputs_cache = (mtime, inputs)
        return inputs

    def _build_accounts_info(
        self, *, load_idle: bool | set[str] = True
    ) -> list[tuple[int, str, str, str, bool, str, str]]:
        """Build per-account (num, email, org_name, org_uuid, is_active, creds, alias).

        Shared by list_accounts and the usage-aware switch helpers so the active
        slot is detected and credentials are read in exactly one place. The
        active account's credentials come from Claude Code's live store; idle
        slots read their backup copy only when ``load_idle`` says so.

        ``load_idle=True`` reads every idle backup (CLI list / on-demand fetch).
        ``load_idle=set()`` (store-only extra paint) reads none of them.
        A non-empty set reads only those idle slot numbers. Live is always
        one credential read. Unread idle creds are ``UNREAD_CREDENTIALS``,
        never ``""``.
        """
        data = self._get_sequence_data_migrated() or {}
        current_identity = self._get_current_account()

        # Find active account number by (email, organizationUuid) composite key
        active_num = None
        if current_identity is not None:
            current_email, current_org_uuid = current_identity
            active_num = self._find_account_slot(data, current_email, current_org_uuid)

        accounts_info: list[tuple[int, str, str, str, bool, str, str]] = []
        # Reset each build; set below only when the active slot's OAuth Keychain
        # read failed with no fallback. Read by _static_usage_sentinel (main
        # thread writes it here before the fetch pool starts → no data race).
        self._record_active_verdict(None)
        self._record_active_backup_fallback(False)
        for num in data.get("sequence", []):
            account = data.get("accounts", {}).get(str(num), {})
            email = account.get("email", "unknown")
            org_name = account.get("organizationName", "") or ""
            org_uuid = account.get("organizationUuid", "") or ""
            alias = account.get("alias", "") or ""
            is_active = str(num) == active_num

            if is_active:
                may_load_active_backup = load_idle is True or (
                    isinstance(load_idle, set) and str(num) in load_idle
                )
                creds = self._active_usage_credentials(
                    str(num), email, load_backup=may_load_active_backup
                )
            elif load_idle is True or (
                isinstance(load_idle, set) and str(num) in load_idle
            ):
                creds = self._read_account_credentials(str(num), email)
            else:
                creds = UNREAD_CREDENTIALS

            accounts_info.append((num, email, org_name, org_uuid, is_active, creds, alias))
        return accounts_info

    def _active_usage_credentials(
        self, account_num: str, email: str, *, load_backup: bool
    ) -> str:
        """Prepare the active credential source for one usage collection.

        Claude Code can leave a syntactically present OAuth record with both
        tokens blank after an ``invalid_grant``. A network-capable collection
        may then use this slot's saved credential; its worker attempts a live
        restore only after identity verification and locked drift checks.
        Store-only paints pass ``load_backup=False`` and retain their no-backup
        I/O guarantee. Shared by full snapshots and active-only status.
        """
        self._record_active_backup_fallback(False)
        active = self._read_active_credentials()
        creds = active.value or ""
        self._record_active_verdict(active)
        if (
            load_backup
            and not active.degraded
            and active.value is not None
            and not looks_like_api_key(creds)
            and not oauth.extract_access_token(creds)
        ):
            backup, backup_unreadable = self._read_account_credentials_ex(
                account_num, email
            )
            if not backup_unreadable and oauth.extract_access_token(backup):
                creds = backup
                self._record_active_backup_fallback(True)
        return creds

    def _fetch_active_usage(
        self, account_num: str, email: str, creds: str, org_uuid: str = ""
    ) -> FetchRecord:
        """Usage fetch for the active/default account, refreshing an expired
        token under Claude Code's own lock protocol.

        Claude Code 2.1.218 is built to *adopt* an externally rotated
        credential rather than collide with it: its refresh takes the
        ``.oauth_refresh.lock`` + legacy ``.claude.lock`` pair, re-reads the
        store under the lock, and skips the network call when the token
        already changed (race-resolved); its 401 path re-reads the store
        before forcing re-auth. So a rotation performed under those same
        locks — re-check, POST, persist, release, all inside — is serialized
        against a live Claude Code and then adopted by it. An owner being
        present is therefore no longer a reason to leave an expired token
        dead (the old behavior stranded idle machines: the owner never
        refreshed, and the dead token 401'd the identity probe, cascading
        into ``unresolved`` switch bounces).

        Two invariants:

        - **Provenance (issue #117)**: a live credential is only CONSUMED
          (its grant POSTed) or WRITTEN into the slot backup when its
          lineage is attributed to the slot — backup-lineage match, a
          profile-oracle verdict from the fresh pass (see
          ``_resync_rotated_backup``), or a refresh POST of our own (memoed
          in ``_probe_verdicts``). Unattributable live bytes are never
          consumed or persisted — but when they are *dead* (expired) and
          the slot's own backup still holds a usable credential, the backup
          is restored to the live store: the backup is by definition the
          slot's credential, so no foreign lineage can be poisoned by it
          (measured field case: a stale cross-machine sync landing an
          already-superseded credential).
        - **Never discard a consumed generation**: once the refresh grant is
          POSTed, the successor is persisted unconditionally (active store +
          slot backup; backup even survives a failing live write). A consumed
          generation left as the live credential is the account-death shape —
          the token endpoint rejects its reuse (verified: invalid_grant on
          re-presentation, siblings unaffected).
        """
        oauth_data = oauth.extract_oauth_data(creds)
        if not oauth_data or not oauth_data.get("accessToken"):
            return FetchRecord(sentinel=USAGE_NO_CREDENTIALS)

        # Every defer before the grant is consumed routes through this: a
        # genuinely expired token earns the sentinel, but a locally-valid
        # server-401'd one (force_refresh set) must surface its 401 record —
        # the store then paces retries with backoff/strike accounting instead
        # of "token expired" mislabeling an unexpired token.
        def _defer(record: "FetchRecord | None") -> "FetchRecord":
            return record or FetchRecord(sentinel=USAGE_TOKEN_EXPIRED)

        # The CONSUME LOCK, taken in the gate's own order (consume -> global).
        # This path can POST the slot's BACKUP refresh token — refresh_input
        # becomes `backup` when the live bytes moved or were cleared — so it
        # is a second backup-token POST outside consume_backup_grant, and the
        # gate's mutual exclusion was not total. Measured: with
        # .consume-N.lock held by another process, this still POSTed.
        #
        # The interleaving it closes: is_active is decided once per collect
        # pass, so a pass that started before a `openswap switch` routes slot N
        # through the gate while a later pass treats N as active and arrives
        # here. The gate releases the global lock across its POST by design,
        # so this path could take it, read the same lineage and POST it too —
        # one wins, the loser gets invalid_grant and strikes a live account.
        force_refresh: FetchRecord | None = None
        if not oauth.is_oauth_token_expired(oauth_data.get("expiresAt")):
            outcome = oauth.try_fetch_usage_for_account(
                account_num, email, creds, is_active=True,
            )
            if outcome.error != "http-401":
                if outcome.usage is not None:
                    # The server just accepted this credential. If its
                    # lineage differs from the slot backup, CC rotated during
                    # normal use and nothing resynced the backup
                    # (rotation-before-collection): the backup holds the
                    # consumed predecessor, and at the next expiry the
                    # recovery branch would POST that dead grant —
                    # invalid_grant, and a healthy slot quarantined. Resync
                    # now, under the same guards as the adopt branch — but
                    # never from a DEGRADED read: the keychain-fallback
                    # plaintext may itself be the consumed predecessor
                    # (still server-valid for its access-token tail), and
                    # writing it would clobber a fresher backup. Serving is
                    # fine; writing waits for a non-degraded pass.
                    if not self._active_read_degraded:
                        self._resync_rotated_backup(
                            account_num, email, org_uuid, creds
                        )
                    if self._probe_verdicts and self._probe_verdicts.get(
                        self._lineage_key(
                            account_num, email,
                            oauth.credential_fingerprint(creds) or "",
                        )
                    ) is False:
                        # The probe just proved the served credential is
                        # another account's: its quota is not this slot's,
                        # and recording it would poison history and switch
                        # decisions (#117's mis-keying shape). The sentinel
                        # reads as unknown headroom to autoswitch, whose
                        # failover switch stashes the foreign credential
                        # and restores the slot's backup — the repair.
                        return FetchRecord(
                            sentinel=USAGE_FOREIGN_CREDENTIAL
                        )
                return FetchRecord(
                    usage=outcome.usage,
                    error=outcome.error,
                    retry_after_s=outcome.retry_after_s,
                )
            # A locally-valid token the server rejects: revoked out-of-band
            # (measured: a sibling machine rotating a synced lineage kills
            # the predecessor access token before its expiresAt) or clock
            # skew. Mirror CC's own 401 reaction — refresh — instead of
            # letting the store's failure backoff loop a dead token for
            # hours until it expires locally. Kept as the fallback record:
            # when no recovery path exists the 401 must reach the store as
            # an ERROR (backoff, strike accounting), not a "token expired"
            # sentinel mislabeling an unexpired token.
            force_refresh = FetchRecord(
                error=outcome.error, retry_after_s=outcome.retry_after_s,
            )

        # Expired (or server-rejected). Before any recovery that would
        # CONSUME a refresh token: a degraded read (the OAuth Keychain
        # failed and a fallback covered it) may be serving a stale
        # generation — on macOS Claude Code rotates keychain-only, so the
        # plaintext file and the slot backup can both hold the consumed
        # predecessor and AGREE with each other. POSTing that rt would
        # yield invalid_grant and a false dead-token strike on a live
        # account (measured field incident). Adopt/serve stays allowed
        # above; consumption is refused until the keychain reads again —
        # CC refreshes on its own next use, exactly the pre-#167 shape.
        if self._active_read_degraded:
            return _defer(
                force_refresh
                or FetchRecord(sentinel=USAGE_KEYCHAIN_UNAVAILABLE)
            )

        # Store-resolution parity (M4), same refusal as the consume gate:
        # with CLAUDE_SECURESTORAGE_CONFIG_DIR set, CC reads/writes a
        # redirected store while this path resolves the default one (capture
        # mirrors it since #205; this one does not) — the copy about to be
        # consumed is the stale predecessor by construction.
        # Serving usage on a still-valid token (above) is fine; consuming
        # or persisting against the left-behind store is not. The distinct
        # kind surfaces the remedy (ERROR_NOTES) instead of striking a
        # healthy account.
        if os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR"):
            self._logger.warning(
                "CLAUDE_SECURESTORAGE_CONFIG_DIR is set; openswap mirrors it "
                "when capturing a credential but not when refreshing one, "
                "so refusing to refresh account %s's active credential "
                "(unset the variable or run from a normal shell).",
                account_num,
            )
            return FetchRecord(error="store-unmirrored")

        # Attribution against the slot's
        # stored backup decides HOW to recover, never whether to give up
        # outright: attributable live → refresh it; unattributable live but
        # usable backup → restore the backup (the slot's own credential —
        # the stranded-live and stale-sync shapes both heal here).
        backup = self._read_account_credentials(account_num, email)
        backup_fp = oauth.credential_fingerprint(backup)
        backup_oauth = oauth.extract_oauth_data(backup)
        backup_usable = bool(
            backup_oauth
            and backup_oauth.get("accessToken")
            and backup_oauth.get("refreshToken")
        )
        attributable = creds == backup or (
            oauth.credential_fingerprint(creds) == backup_fp
        )
        if not attributable and not backup_usable:
            # Nothing safe to consume and nothing to restore from. Warn once
            # per condition, not per collect pass.
            if (account_num, email, "unattributable") not in self._provenance_warned:
                self._provenance_warned.add((account_num, email, "unattributable"))
                self._logger.warning(
                    "Active credential does not match Account-%s's stored "
                    "backup and the backup is unusable; cannot refresh "
                    "(provenance unknown).",
                    account_num,
                )
            return _defer(force_refresh)
        self._provenance_warned.discard((account_num, email, "unattributable"))

        # Claude Code's own sequence: locks → re-read → decide → POST →
        # persist unconditionally → release. A concurrently refreshing CC is
        # serialized here and adopts our rotation on its next locked re-read.
        try:
            # Lock order matches the switch path (switch_to): openswap's own
            # account lock first, then Claude Code's. FileLock excludes
            # concurrent swap/move relocations (their docstring relies on
            # usage-refresh persists taking this lock); the CC pair excludes
            # a concurrently refreshing Claude Code. Nothing inside
            # re-acquires FileLock, so the a07c767 non-reentrancy hazard
            # does not apply.
            # The config lock is NOT taken here: CC holds only the
            # credential locks across its POST, and the config lock guards a
            # local ~/.claude.json RMW with a ~10s retry budget on CC's side
            # — holding it through a slow POST could exhaust a concurrent CC
            # config save's retries. It is narrowed to the live-store write
            # below (the one step that can touch ~/.claude.json).
            with (
                FileLock(self.credentials_dir / f".consume-{account_num}.lock"),
                FileLock(self.lock_file),
                claude_credentials_lock(),
            ):
                live = self._read_credentials()
                if live is None:
                    # Read ERROR (locked keychain, unreadable store) — not
                    # absence. The store may hold a newer credential we
                    # cannot see; guessing here could consume a superseded
                    # grant. Defer to the next pass.
                    return _defer(force_refresh)
                live_oauth = oauth.extract_oauth_data(live) if live else None
                # Under-lock TOCTOU guards. A `openswap switch` or `/login`
                # completing between the pre-lock attribution and lock
                # acquisition replaces the live credential (and the config
                # identity). Two independent checks, because rotation and
                # switching move different markers:
                # - identity (config oauthAccount, email AND organization —
                #   two managed slots may share an email across orgs): a
                #   switch/login changes it; a CC token rotation does not.
                #   Mismatch → the live store now belongs to another account
                #   — nothing here is ours to adopt, consume, or overwrite.
                #   Runs even when the live blob is empty or non-OAuth
                #   (live_oauth None): a switch to an API-key account landing
                #   in the gap leaves exactly that shape. An empty live WITH
                #   our identity (CC cleared the credential) still passes —
                #   that is a recovery case.
                # - lineage (refresh-token fingerprint): decides whether the
                #   live bytes may be CONSUMED or must be replaced from the
                #   backup.
                if not self._live_identity_matches(email, org_uuid):
                    return _defer(force_refresh)
                if (
                    live_oauth
                    and live != creds
                    # A CC invalid_grant wipe empties the token fields in
                    # place but keeps metadata (observed on 2.1.181), and an
                    # external writer can land an accessToken-only blob —
                    # either way a "non-expired" look without a full token
                    # pair must not be adopted (the resync would replace the
                    # backup's only refresh token).
                    and live_oauth.get("accessToken")
                    and live_oauth.get("refreshToken")
                    and not oauth.is_oauth_token_expired(
                        live_oauth.get("expiresAt")
                    )
                ):
                    # Someone (a live CC) already rotated it — adopt, consume
                    # nothing. Mirrors CC's race-resolved path. Resync the
                    # slot backup so the rotated lineage stays attributable
                    # at the NEXT expiry — but only when the lineage is
                    # attributable NOW (backup lineage, or an oracle/memo
                    # verdict): a foreign fresh credential under a stale
                    # config satisfies every local condition here, and
                    # writing it would destroy the slot's refresh token. An
                    # unverified lineage is adopted for usage only (network
                    # is forbidden under these locks); the next collect pass
                    # reads it fresh and the oracle-checked resync heals the
                    # backup one pass late.
                    live_verdict = self._probe_verdicts.get(
                        self._lineage_key(
                            account_num, email,
                            oauth.credential_fingerprint(live) or "",
                        )
                    )
                    if live_verdict is False:
                        # Known-foreign: don't adopt, don't serve usage
                        # mislabeled as this slot's. The foreign sentinel
                        # (not a defer) so autoswitch fails over instead of
                        # idle-holding — the switch is what repairs the
                        # drift.
                        return FetchRecord(
                            sentinel=USAGE_FOREIGN_CREDENTIAL
                        )
                    working = live
                    if live_verdict or (
                        oauth.credential_fingerprint(live) == backup_fp
                    ):
                        try:
                            self._write_account_credentials(
                                account_num, email, live
                            )
                        except Exception:
                            self._logger.warning(
                                "Backup resync after adopting a rotated "
                                "credential failed for account %s; the next "
                                "expiry may refuse to refresh until a "
                                "switch resyncs it.", account_num,
                            )
                    else:
                        self._logger.debug(
                            "Adopted a rotated live credential for account "
                            "%s without a lineage verdict; backup resync "
                            "deferred to the next fresh pass's oracle "
                            "check.", account_num,
                        )
                else:
                    # Pick the credential whose grant may be consumed:
                    # - live, when its lineage matches the backup (rotated /
                    #   drifted bytes of this slot);
                    # - the backup itself, when the live bytes moved to a
                    #   foreign-but-dead lineage or were cleared (restore);
                    # - what the collector read, as the last resort.
                    # Foreign live bytes that appeared only mid-flight (live
                    # differs from what the collector read AND from the
                    # backup lineage) mean an actor is mutating the store
                    # right now — defer rather than fight it.
                    restore_source = None
                    if live_oauth is not None and (
                        oauth.credential_fingerprint(live) == backup_fp
                    ):
                        # Live is the slot's own lineage (possibly drifted) —
                        # its bytes are the freshest copy of the grant.
                        refresh_input = (
                            live if live_oauth.get("refreshToken") else
                            (backup if backup_usable else creds)
                        )
                    elif not live:
                        # CC cleared the live store — recover from the
                        # backup's grant.
                        refresh_input = backup if backup_usable else creds
                    elif live == creds:
                        # Nothing moved since the collector read, but the
                        # bytes don't match the backup's lineage. The
                        # generation ordering (expiresAt moves forward on
                        # every rotation) SELECTS a candidate, but only an
                        # ownership verdict LICENSES consuming it:
                        # - backup newer → live is a stranded consumed
                        #   generation or a stale external sync; the backup
                        #   is the slot's real credential — restore or
                        #   refresh from it, never POST the dead live grant.
                        # - live newer AND this process attributed the
                        #   lineage (own refresh POST, or a fresh-pass
                        #   oracle match whose backup write failed) — POST
                        #   live, the valid successor.
                        # - live newer but unattributed → could equally be a
                        #   foreign credential under a stale config; POSTing
                        #   would consume another machine's grant. Defer to
                        #   CC's next use (pre-#167 behavior for this shape
                        #   — the slept-through-rotation and cross-process
                        #   drift subcases give up auto-heal by design).
                        live_exp = (live_oauth or {}).get("expiresAt") or 0
                        backup_exp = (
                            backup_oauth.get("expiresAt") or 0
                            if backup_oauth else 0
                        )
                        if (
                            live_oauth
                            and live_oauth.get("refreshToken")
                            and live_exp > backup_exp
                        ):
                            if self._probe_verdicts.get(
                                self._lineage_key(
                                    account_num, email,
                                    oauth.credential_fingerprint(live) or "",
                                )
                            ):
                                refresh_input = live
                            else:
                                key = (account_num, email,
                                       "expiry-unattributed")
                                if key not in self._provenance_warned:
                                    self._provenance_warned.add(key)
                                    self._logger.warning(
                                        "Live credential is newer than "
                                        "Account-%s's backup but its "
                                        "ownership is unverified; refresh "
                                        "deferred to Claude Code's next "
                                        "use.", account_num,
                                    )
                                return _defer(force_refresh)
                        else:
                            refresh_input = backup if backup_usable else creds
                    else:
                        # Live moved mid-flight to bytes that are neither
                        # what the collector read nor the backup's lineage —
                        # another actor is mutating the store; defer.
                        return _defer(force_refresh)
                    input_oauth = oauth.extract_oauth_data(refresh_input)
                    if (
                        refresh_input == backup
                        and backup_usable
                        and not force_refresh
                        and input_oauth
                        and not oauth.is_oauth_token_expired(
                            input_oauth.get("expiresAt")
                        )
                    ):
                        # The backup already holds a live, non-expired
                        # credential (a prior locked refresh persisted it but
                        # the live write failed, stranding the live store on
                        # the consumed generation). Restore it — no POST, no
                        # generation consumed.
                        restore_source = backup
                        working = backup
                    else:
                        # The POST runs while holding the account FileLock
                        # (contended by `openswap switch` with a 10s acquire
                        # budget) and CC's credential locks. Bound it well
                        # inside that budget so a slow network can't make a
                        # concurrent switch's acquire expire — the switch
                        # then waits out the tail instead of erroring.
                        result = oauth.try_refresh_oauth_credentials(
                            refresh_input, timeout_s=6.0
                        )
                        if result.error in (
                            "invalid_grant", "no_refresh_token"
                        ) or (
                            result.error is None and not result.credentials
                        ):
                            # Permanently unrefreshable: dead lineage or a
                            # credential with no refresh token at all.
                            # Demotion check before condemning: re-read the
                            # SOURCE the POSTed bytes came from — a lineage
                            # that moved while our POST was in flight means
                            # we consumed a superseded copy (a writer raced
                            # us), which is evidence about OUR bytes, not
                            # the slot. Compare like with like: live-sourced
                            # input against the live store, backup-sourced
                            # input against the backup (comparing a backup
                            # input to the live store reads "moved" on every
                            # pass by construction — a permanent false
                            # negative that would keep a dead lineage out
                            # of quarantine forever). Record transient; the
                            # next pass consumes the newer lineage. (#121
                            # discipline: local reads only, no network,
                            # failure degrades to today.)
                            try:
                                if refresh_input == backup:
                                    source_now = (
                                        self._read_account_credentials(
                                            account_num, email
                                        )
                                    )
                                else:
                                    source_now = (
                                        self._read_credentials() or ""
                                    )
                                moved = (
                                    bool(source_now)
                                    and oauth.credential_fingerprint(
                                        source_now
                                    )
                                    != oauth.credential_fingerprint(
                                        refresh_input
                                    )
                                )
                            except Exception:
                                moved = False
                            if moved:
                                return FetchRecord(error="refresh-failed")
                            # Surface as an ERROR so the store advances auth
                            # strikes, applies backoff, and the quarantine
                            # scan flips the account to "re-login needed" —
                            # a bare sentinel is a no-op to the store and
                            # would re-POST every pass. The strike binds to
                            # the consumed generation's fingerprint.
                            return FetchRecord(
                                error=result.error or "invalid_grant",
                                struck_fp=oauth.credential_fingerprint(
                                    refresh_input
                                ),
                            )
                        if result.error is not None:
                            # Transient (network) failure: backoff via store.
                            return FetchRecord(error="refresh-failed")
                        working = result.credentials
                        # Our own POST produced this lineage — self-attributed,
                        # no oracle needed. The verdict is what lets the next
                        # expiry consume it if the backup write below fails.
                        self._probe_verdicts[
                            self._lineage_key(
                                account_num, email,
                                oauth.credential_fingerprint(working or "")
                                or "",
                            )
                        ] = True
                        self._provenance_warned.discard(
                            (account_num, email, "expiry-unattributed")
                        )
                    # The credential must reach the stores — after a POST the
                    # grant is consumed and the successor MUST survive in at
                    # least one of them. Attempt both; tolerate either
                    # failing alone. (For a restore, the backup already holds
                    # it; only the live store needs the write.)
                    backup_ok = live_ok = True
                    if restore_source is None:
                        try:
                            self._write_account_credentials(
                                account_num, email, working
                            )
                        except Exception:
                            backup_ok = False
                            self._logger.warning(
                                "Backup write failed after a consumed "
                                "refresh for account %s; attempting the "
                                "active store.",
                                account_num,
                            )
                    try:
                        # _write_credentials can touch ~/.claude.json (via
                        # _clear_managed_key) — the config lock covers just
                        # this write. A timeout here is a live-write failure
                        # (the grant is already consumed), not a defer.
                        with claude_config_lock():
                            self._write_credentials(working)  # active store — CC reads this
                    except Exception:
                        live_ok = False
                        self._logger.warning(
                            "Active-store write failed after a %s for "
                            "account %s%s.",
                            "backup restore" if restore_source is not None
                            else "consumed refresh",
                            account_num,
                            "" if backup_ok
                            else "; the rotated credential was NOT persisted "
                                 "anywhere — re-login may be required",
                        )
                    if not live_ok:
                        # Live still holds the dead token — don't serve
                        # usage for a credential CC can't currently use.
                        return FetchRecord(sentinel=USAGE_TOKEN_EXPIRED)
        except LockError:
            # A live holder — Claude Code mid-refresh (ClaudeCodeLockTimeout)
            # or another openswap operation holding the account FileLock. Either
            # way the credential is being handled; try again next tick rather
            # than steal, wait unboundedly, or raise through the never-raises
            # fetch contract.
            self._logger.info(
                "Credential locks held elsewhere; deferring the "
                "active-token refresh for account %s to the next pass.",
                account_num,
            )
            return _defer(force_refresh)
        except Exception:
            # _fetch_account_usage promises never to raise into the collect
            # pass (a raising worker would kill the whole pass for every
            # account). Config/lock-file I/O errors land here.
            self._logger.warning(
                "Active-token refresh for account %s failed unexpectedly; "
                "deferring to the next pass.", account_num, exc_info=True,
            )
            return _defer(force_refresh)

        outcome = oauth.try_fetch_usage_for_account(
            account_num, email, working, is_active=True,
        )
        return FetchRecord(
            usage=outcome.usage,
            error=outcome.error,
            retry_after_s=outcome.retry_after_s,
        )

    def _resync_rotated_backup(
        self, account_num: str, email: str, org_uuid: str, creds: str
    ) -> None:
        """Resync the slot backup after a rotation that completed elsewhere.

        The fresh-token fast path serves usage off a credential the server
        just accepted. When that credential's lineage differs from the slot
        backup, Claude Code rotated during normal use and nothing resynced
        the backup (rotation-before-collection): the backup still holds the
        consumed predecessor, and the next expiry's recovery branch would
        POST that dead grant — invalid_grant on a healthy slot. This is the
        adopt-branch resync extended to a rotation that already completed:
        same identity re-check, same full-token-pair guard, same locks.

        The config identity alone cannot attribute the drifted bytes: a
        foreign credential can occupy the live store while ``~/.claude.json``
        still names this slot (partial cross-machine sync of the credential
        store; a poll landing inside ``/login``'s non-atomic write), and
        writing those bytes would destroy the slot's only surviving refresh
        token. So the drifted lineage — including the empty-backup *seeding*
        case — must be attributed by the profile oracle before it is
        persisted: probed here, before any lock (network under locks is
        forbidden), while the access token is known-fresh (the usage
        endpoint just accepted it). Definitive verdicts are memoized per
        lineage so a persistent drift state doesn't re-probe every pass.

        Best-effort: any failure (lock contention, read error, identity
        moved, oracle unreachable) just leaves the backup stale — the
        recovery branch consumes nothing it cannot attribute. Never raises.
        """
        try:
            creds_oauth = oauth.extract_oauth_data(creds)
            if not (
                creds_oauth
                and creds_oauth.get("accessToken")
                and creds_oauth.get("refreshToken")
            ):
                return  # never seed a backup with a partial token pair
            backup = self._read_account_credentials(account_num, email)
            if backup and (
                oauth.credential_fingerprint(creds)
                == oauth.credential_fingerprint(backup)
            ):
                return  # same lineage — nothing drifted
            fp = oauth.credential_fingerprint(creds) or ""
            verdict = self._probe_verdicts.get(
                self._lineage_key(account_num, email, fp)
            )
            if verdict is False:
                return  # known-foreign lineage; already warned
            if verdict is not True:
                resolved = oauth.fetch_oauth_profile(
                    oauth.extract_access_token(creds) or ""
                )
                if resolved is None:
                    self._logger.debug(
                        "Ownership probe for account %s's drifted live "
                        "credential failed; resync skipped this pass.",
                        account_num,
                    )
                    return
                match = self._resolved_matches_slot_identity(
                    account_num, resolved
                )
                if match is None:
                    self._logger.debug(
                        "Ownership of account %s's drifted live credential "
                        "is unverifiable (no stored uuid, partial profile); "
                        "resync skipped this pass.",
                        account_num,
                    )
                    return
                # Key built AFTER the match: an email-path affirmation just
                # backfilled the slot uuid, and the verdict must live under
                # the identity consults will rebuild from now on.
                self._probe_verdicts[
                    self._lineage_key(account_num, email, fp)
                ] = match
                if not match:
                    key = (account_num, email, "resync")
                    if key not in self._provenance_warned:
                        self._provenance_warned.add(key)
                        self._logger.warning(
                            "Live credential resolves to a different "
                            "account than Account-%s's identity; backup "
                            "left untouched (foreign credential under a "
                            "stale config).",
                            account_num,
                        )
                    return
                self._provenance_warned.discard((account_num, email, "resync"))
            with (
                FileLock(self.lock_file),
                claude_credentials_lock(),
            ):
                # Identity re-check under the lock: a switch/login landing in
                # the gap means the live store is no longer this account's.
                if not self._live_identity_matches(email, org_uuid):
                    return
                # Verdict re-check under the lock: slot mutations hold this
                # FileLock, so rebuilding the key revalidates that the slot
                # still IS the account the oracle affirmed.
                if not self._probe_verdicts.get(
                    self._lineage_key(account_num, email, fp)
                ):
                    return
                # Re-read live under the lock and require it to still carry
                # the served (and oracle-attributed) credential's lineage
                # with a full pair — the fingerprint covers the refresh
                # token, so the bytes written share the probed lineage even
                # if the access token moved since the probe.
                live = self._read_credentials()
                if not live:
                    return
                live_oauth = oauth.extract_oauth_data(live)
                if not (
                    live_oauth
                    and live_oauth.get("accessToken")
                    and live_oauth.get("refreshToken")
                    and oauth.credential_fingerprint(live)
                    == oauth.credential_fingerprint(creds)
                ):
                    return
                self._write_account_credentials(account_num, email, live)
                self._logger.info(
                    "Resynced account %s's backup to the rotated live "
                    "credential (rotation completed outside a collect pass).",
                    account_num,
                )
        except LockError:
            return  # holder is mid-operation; the next pass retries
        except Exception:
            self._logger.warning(
                "Backup resync for account %s failed; the recovery branch's "
                "newer-generation check still guards the next expiry.",
                account_num, exc_info=True,
            )

    def _static_usage_sentinel(
        self, account_info: tuple[int, str, str, str, bool, str, str]
    ) -> str | None:
        """Sentinel state derivable without any network call, or ``None``.

        Re-derived on every collect pass (never persisted), so it can't
        outlive the condition that produced it.
        """
        num, email, _, _, is_active, creds, _alias = account_info
        if creds is UNREAD_CREDENTIALS:
            # Roster-only paint: do not classify unread as empty. Kind comes
            # from sequence.json so an idle API-key slot still shows "api key".
            if self._account_kind(str(num)) == "api_key":
                return USAGE_API_KEY
            return None
        if looks_like_api_key(creds):
            # Managed API-key account: no subscription quota to fetch.
            return USAGE_API_KEY
        if not creds or not oauth.extract_access_token(creds):
            if is_active and (
                self._active_keychain_unavailable or self._active_read_unreadable
            ):
                return USAGE_KEYCHAIN_UNAVAILABLE
            if not is_active and self._read_account_credentials_ex(
                str(num), email
            )[1]:
                # THIS slot's own read, not the process flag — which one
                # slot's clean read erased for every other slot. Measured with
                # every read denied and a real backup on slot 2:
                #
                #     before   slot2='no credentials'   slot9='no credentials'
                #     after    slot2='keychain unavailable'
                #
                # "no credentials" sends the user to re-add a slot that has one
                # — the dead end 41313b9 removed from three other sites.
                return USAGE_KEYCHAIN_UNAVAILABLE
            return USAGE_NO_CREDENTIALS
        # An expired active token is no longer a static state: the fetch path
        # refreshes it under Claude Code's own lock protocol (owner or not),
        # so the collect pass must reach it rather than short-circuit here.
        # USAGE_TOKEN_EXPIRED now only surfaces from the fetch path itself
        # (unattributable lineage, dead lineage, lock contention, failed
        # persist) — states that genuinely need the autoswitch ladder.
        return None

    def _fetch_account_usage(
        self, account_info: tuple[int, str, str, str, bool, str, str]
    ) -> FetchRecord:
        """One network fetch for one account. Never raises."""
        num, email, _, org_uuid, is_active, creds, _alias = account_info

        # The active/default account owns the live credential — route it
        # through the locked-refresh path (refreshes an expired token under
        # Claude Code's own lock protocol, owner or not).
        if is_active:
            if self._active_backup_fallback():
                repair = self._auto_restore_missing_active_credential(
                    str(num), email, org_uuid, creds
                )
                if repair == "foreign":
                    return FetchRecord(sentinel=USAGE_FOREIGN_CREDENTIAL)
                outcome = oauth.try_fetch_usage_for_account(
                    str(num), email, creds, is_active=True,
                )
                return FetchRecord(
                    usage=outcome.usage,
                    error=outcome.error,
                    retry_after_s=outcome.retry_after_s,
                )
            return self._fetch_active_usage(str(num), email, creds, org_uuid)

        from openswap.session import (
            read_session_credentials,
            session_identity_drifted,
        )

        has_live_session = bool(self._live_session_pids(str(num), email))

        # A session profile supersedes the backup copy as this account's
        # credential truth: claude rotates the token family inside the profile
        # and the backup only catches up once the session exits (adopted
        # below), so while one is live the backup's refresh token is a
        # consumed generation the server 401s forever — usage would silently
        # freeze at the last pre-session measurement. Fetch with the profile's
        # newest credential, strictly read-only (is_active=True: no refresh,
        # no persist): rotating the profile's family here would log the live
        # claude out the same way.
        session_dir = self._session_dir(str(num), email)
        session_creds = read_session_credentials(session_dir)
        if session_creds and session_identity_drifted(session_dir, email, org_uuid):
            # An in-session /login re-pointed the profile at a different
            # account; fetching with its credential would record THAT
            # account's usage under this slot's label. The profile no longer
            # holds this slot's token family, so the backup below is both the
            # right identity and safe to refresh — treat the slot as not
            # session-owned for this fetch.
            self._logger.debug(
                f"Session profile for account {num} is logged in as a "
                f"different account; fetching usage from the backup credential"
            )
            session_creds = None
            has_live_session = False
        if session_creds and not has_live_session:
            # The session has exited, so its family is nobody's to rotate but
            # the store's: adopt the profile head into the backup (which is a
            # consumed generation until then) and take the idle path below,
            # refresh included, on that credential. A profile already on the
            # backup's generation, or behind a fresh re-login in the backup,
            # needs no adoption and takes the same path on the backup. An
            # adoption refused for any other reason (lock contention, a
            # session record that could not be read) leaves both copies as
            # they were.
            try:
                if self._adopt_session_credential(str(num), email, org_uuid):
                    creds = session_creds
            except LockError:
                pass
            session_creds = None
        if session_creds:
            session_oauth = oauth.extract_oauth_data(session_creds)
            if session_oauth and session_oauth.get("accessToken"):
                if not oauth.is_oauth_token_expired(session_oauth.get("expiresAt")):
                    outcome = oauth.try_fetch_usage_for_account(
                        str(num), email, session_creds, is_active=True,
                    )
                    return FetchRecord(
                        usage=outcome.usage,
                        error=outcome.error,
                        retry_after_s=outcome.retry_after_s,
                    )
                # The live claude refreshes lazily on its next API call;
                # requesting now would just 401 (same rule as the owned
                # active account in _fetch_active_usage).
                return FetchRecord(sentinel=USAGE_TOKEN_EXPIRED)

        outcome = oauth.try_fetch_usage_for_account(
            str(num), email, creds,
            is_active=has_live_session,
            refresh_via=(
                None if has_live_session else self.consume_backup_grant
            ),
        )
        return FetchRecord(
            usage=outcome.usage,
            error=outcome.error,
            retry_after_s=outcome.retry_after_s,
            struck_fp=outcome.struck_fp,
        )

    def _auto_restore_missing_active_credential(
        self, account_num: str, email: str, org_uuid: str, backup: str
    ) -> str:
        """Restore a verified saved credential into an empty live store.

        Returns ``"restored"``, ``"deferred"``, or ``"foreign"``. Identity
        verification happens before the locks (network is forbidden while
        held), then the live identity, credential, and backup generation are
        all re-read under the same lock order as a normal switch. Any drift
        defers to a later pass instead of overwriting it.
        """
        token = oauth.extract_access_token(backup)
        fingerprint = oauth.credential_fingerprint(backup) or ""
        if not token or not fingerprint:
            return "deferred"

        key = self._lineage_key(account_num, email, fingerprint)
        verdict = self._probe_verdicts.get(key)
        if verdict is None:
            resolved = oauth.fetch_oauth_profile(token)
            if not resolved:
                return "deferred"
            verdict = self._resolved_matches_slot_identity(account_num, resolved)
            if verdict is not None:
                self._probe_verdicts[key] = verdict
        if verdict is False:
            self._logger.warning(
                "Saved credential for account %s belongs to another account; "
                "automatic live-login repair refused.", account_num,
            )
            return "foreign"
        if verdict is not True:
            return "deferred"

        try:
            with (
                FileLock(self.lock_file),
                claude_credentials_lock(),
                claude_config_lock(),
            ):
                if not self._live_identity_matches(email, org_uuid):
                    return "deferred"
                live = self._read_credentials()
                if live is None:
                    return "deferred"
                if looks_like_api_key(live) or oauth.extract_access_token(live):
                    return "deferred"
                current_backup, unreadable = self._read_account_credentials_ex(
                    account_num, email
                )
                if unreadable or (
                    oauth.credential_fingerprint(current_backup) != fingerprint
                ):
                    return "deferred"
                self._write_credentials(
                    self._prepare_credentials_for_activation(current_backup, live)
                )
        except LockError:
            return "deferred"
        except Exception:
            self._logger.warning(
                "Automatic live-login repair for account %s failed; leaving "
                "the saved credential untouched for manual recovery.",
                account_num, exc_info=True,
            )
            return "deferred"

        self._mark_active_backup_repaired()
        self._logger.info(
            "Restored account %s's verified saved credential into the empty "
            "live Claude store.", account_num,
        )
        return "restored"

    def _run_usage_fetches(
        self, infos: list[tuple[int, str, str, str, bool, str, str]]
    ) -> dict[str, FetchRecord]:
        """Fetch the given accounts in parallel, staggering request starts so
        N accounts never hit the endpoint in the same instant."""
        def fetch_one(
            idx_info: tuple[int, tuple[int, str, str, str, bool, str, str]]
        ) -> tuple[str, FetchRecord]:
            idx, info = idx_info
            if idx and _FETCH_STAGGER_S:
                time.sleep(idx * _FETCH_STAGGER_S)
            return str(info[0]), self._fetch_account_usage(info)

        with ThreadPoolExecutor() as executor:
            return dict(
                executor.map(self._with_active_verdict(fetch_one), enumerate(infos))
            )

    def _collect_usage_entries(
        self,
        accounts_info: list[tuple[int, str, str, str, bool, str, str]],
        fetch: set[str] | None = None,
        *,
        scheduled: bool = False,
    ) -> dict[str, UsageEntry]:
        """Store-backed usage collection: one :class:`UsageEntry` per account.

        ``fetch=None`` (on-demand callers: ``--list``/``--status``/switch
        strategies, dashboards) makes every account a candidate but respects
        the persisted poll plans; the auto engine passes an explicit set whose
        members may beat the serve TTL when their plan says so (urgent
        cadence) or, unless ``scheduled`` is set, when escalation needs them
        fresh. Final eligibility —
        freshness, backoff, claims, plans — is decided atomically by
        ``UsageStore.reserve``, so concurrent collectors can never
        double-fetch a slot. After each successful fetch the adapted cadence
        is persisted (``_persist_poll_plans``), making every surface inherit
        the same plan. A failed fetch only updates the entry's error/backoff
        fields, so the last-good measurement keeps being served
        (stale-on-error).
        """
        store = self._usage_store
        identities = {
            str(num): (email, org_uuid or "")
            for num, email, _org_name, org_uuid, _active, _creds, _alias in accounts_info
        }
        info_by_num = {str(info[0]): info for info in accounts_info}
        # Scoped-window models so the 429-stale trust bound honors per-model
        # (e.g. Fable) resets, matching the poll planner's window view.
        _threshold, models = self._poll_policy_inputs()
        sentinels: dict[str, str] = {}
        for num, info in info_by_num.items():
            static = self._static_usage_sentinel(info)
            if static is not None:
                sentinels[num] = static

        entries = store.entries(identities, models)
        # Dead refresh-token lineage: quarantine. Surfacing the sentinel here both
        # drives the "re-login needed" display and (via ``num not in sentinels``
        # below) stops the endless fetch loop that would otherwise 401/429 forever.
        probe_backup = fetch is None or bool(fetch)
        for num in info_by_num:
            if num in sentinels:
                continue
            entry = entries[num]
            _i = info_by_num[num]
            if _i[5] is UNREAD_CREDENTIALS:
                continue
            if self._entry_token_dead(
                entry, num, _i[1], _i[5], _i[4], probe_backup=probe_backup
            ):
                sentinels[num] = USAGE_RELOGIN_REQUIRED
            elif entry.auth_dead_strikes and entry.token_dead():
                # Struck, but no stored source still matches the condemned
                # generation — the fingerprint healed the verdict.
                # Clear the stale strike ROW too: display and fetch
                # eligibility (_row_eligible gates on the raw count) must
                # agree, or the slot silently freezes at last-good.
                self._usage_store.clear_dead_token(
                    [num], {num: identities[num]}
                )
                entries = store.entries(identities, models)
        requested = [
            num
            for num in info_by_num
            if num not in sentinels and (fetch is None or num in fetch)
        ]
        if fetch is None:
            # Repair reset-parked plans written by releases that stopped
            # polling exhausted accounts until their advertised reset. The
            # store recognizes that impossible deadline shape under the same
            # lock that installs the claim, so a concurrent valid replan is
            # never bypassed.
            claims = store.reserve(
                requested,
                identities,
                respect_plans=True,
                repair_overslept=True,
            )
        else:
            claims = store.reserve(
                requested,
                identities,
                respect_plans=False,
                repair_overslept=scheduled,
            )
        # An expired ACTIVE credential that cannot reach the fetch path (and
        # its locked refresh) this tick — failure backoff, a concurrent
        # collector's claim, poll-plan gate — must still surface the expired
        # state so the auto engine idle-holds instead of counting the gap
        # toward a spurious failover (Finding 2). When the gate lifts, the
        # fetch path refreshes the token and the sentinel clears itself.
        for num, info in info_by_num.items():
            if num in sentinels or not info[4]:  # info[4] = is_active
                continue
            if num in claims:
                continue  # the fetch path will handle (or sentinel) it now
            active_oauth = oauth.extract_oauth_data(info[5])
            if active_oauth and oauth.is_oauth_token_expired(
                active_oauth.get("expiresAt")
            ):
                sentinels[num] = USAGE_TOKEN_EXPIRED

        if claims:
            pre = entries
            records = self._run_usage_fetches(
                [info_by_num[num] for num in claims]
            )
            plans = self._plans_after_fetch(records, pre, info_by_num)
            accepted = store.record(records, identities, claims, plans)
            accepted_records = {
                num: record for num, record in records.items() if num in accepted
            }
            for num, record in accepted_records.items():
                if record.sentinel is not None:
                    sentinels[num] = record.sentinel
            entries = store.entries(identities, models)
            # A fetch that just returned invalid_grant advances the strike to the
            # dead threshold. The pre-fetch quarantine scan above couldn't see it,
            # so surface "re-login needed" in *this* pass instead of leaving the
            # slot looking merely refresh-failed until the next refresh notices.
            for num in accepted:
                _i = info_by_num[num]
                if self._entry_token_dead(
                    entries[num], num, _i[1], _i[5], _i[4]
                ):
                    sentinels[num] = USAGE_RELOGIN_REQUIRED

        # A pass using the saved credential must not make the live Claude
        # login look healthy. On success the store now has a current last-good
        # measurement; on failure it keeps the older one. In either case the
        # UI can offer a precise one-click restoration. A stronger sentinel
        # produced by this pass (for example re-login-required) wins.
        if self._active_backup_fallback() and not self._active_backup_repaired():
            for num, info in info_by_num.items():
                if info[4]:
                    sentinels.setdefault(num, USAGE_LIVE_CREDENTIAL_MISSING)
                    break

        return {
            num: with_sentinel(entries[num], sentinels.get(num))
            for num in info_by_num
        }

    def _slot_token_dead(self, num: str, email: str) -> bool:
        """Is this slot quarantined as refresh-token-dead, right now?

        The same question :meth:`_entry_token_dead` answers for the collectors,
        reachable from a caller that has only a slot number — `openswap import`'s
        auto-heal, which must agree with them: the heal exists to release a
        quarantine the collectors imposed, so a different verdict means the
        remedy the "re-login needed" message names silently does nothing.

        In particular the ACTIVE slot has two stored sources, and a strike may
        be bound to either (see :meth:`_entry_token_dead`). Comparing only the
        backup — as the import used to — leaves an active slot struck on its
        live generation unhealable, and that is the slot most likely to be
        quarantined in the first place.
        """
        # The org uuid is part of the row identity (UsageStore._matches
        # compares it for EQUALITY), so an empty one silently matches nothing:
        # every real account carries a non-empty org, and the lookup would
        # return a blank entry whose token_dead() is always False.
        data = self._get_sequence_data() or {}
        org = (
            (data.get("accounts", {}).get(num) or {})
            .get("organizationUuid", "") or ""
        )
        ident = {num: (email, org)}
        entry = self._usage_store.entries(ident).get(num)
        if entry is None:
            return False
        is_active = num == self.current_account_number()
        # The backup is a stored source on BOTH paths — directly when idle,
        # and as _entry_token_dead's second source when active — and each
        # turns an unreadable one into "dead" by a different route: an empty
        # fingerprint skips token_dead's binding check, and the active path
        # deliberately HOLDS a strike it cannot disprove (right for the
        # collectors, wrong here). Both end in `import_accounts` replacing a
        # healthy slot without --force. We cannot see it, so we cannot
        # condemn it.
        backup, unreadable = self._read_account_credentials_ex(num, email)
        if unreadable:
            return False
        # The stored source, as _build_accounts_info reports it: the LIVE
        # credential for the active slot, the backup otherwise. `.value` is
        # tri-state (`""` genuinely absent, `None` a read ERROR) — collapsing
        # it with `or ""` fed `credential_fingerprint("")` (None) into
        # `token_dead`, which treats a None stored_fp as "binds
        # unconditionally" and condemned a slot whose live credential simply
        # could not be read this instant. Same "we cannot see it, we cannot
        # condemn it" rule as the backup guard above.
        if is_active:
            active_value = self._store._read_active_credentials().value
            if active_value is None:
                return False
            stored = active_value
        else:
            stored = backup
        return self._entry_token_dead(entry, num, email, stored, is_active)

    def _entry_token_dead(
        self,
        entry: UsageEntry,
        num: str,
        email: str,
        stored: str,
        is_active: bool,
        *,
        probe_backup: bool = True,
    ) -> bool:
        """Fingerprint-bound dead verdict against EVERY stored source.

        Unread idle backups are not a stored source this pass — do not
        fingerprint them.

        For an idle slot ``info[5]`` is the backup, the only source a strike
        can bind to. The ACTIVE slot has two stored sources — ``info[5]`` is
        the LIVE credential, but ``_fetch_active_usage``'s recovery branch
        legitimately POSTs (and binds the strike to) the slot BACKUP.
        Comparing the strike only against the live bytes mis-heals it on
        every pass whenever the two lineages differ — the strike/heal/re-POST
        loop that keeps a dead backup out of quarantine forever. The strike
        holds while ANY stored source still matches the struck generation.

        ``probe_backup=False`` (store-only paint) skips the backup Keychain
        read and holds an existing strike, same as an unreadable backup.
        """
        if stored is UNREAD_CREDENTIALS:
            return False
        if entry.token_dead(stored_fp=oauth.credential_fingerprint(stored)):
            return True
        if not is_active:
            return False
        if not probe_backup:
            return entry.token_dead()
        backup, unreadable = self._read_account_credentials_ex(num, email)
        if unreadable:
            # The second source cannot be seen, so "no stored source matches
            # the struck generation" is unproven — and the caller's `elif`
            # spends that answer on `clear_dead_token`, which zeroes
            # authDeadStrikes AND struckFingerprint in the PERSISTED store.
            # One momentary lock would un-quarantine a genuinely dead account
            # permanently and resume POSTing its dead grant. Holding the
            # strike costs one pass of "re-login needed" on a row that
            # already took AUTH_DEAD_STRIKES invalid_grants; erasing it costs
            # the quarantine itself.
            #
            # Gated on an actual strike existing (``entry.token_dead()``, no
            # ``stored_fp`` — we can't verify which generation, so this only
            # asks whether the count itself has reached threshold): the
            # first check above already proved the LIVE credential doesn't
            # carry the struck generation, so a row with zero strikes has
            # nothing to hold — an unreadable backup on an otherwise-healthy
            # account must not manufacture "re-login needed" out of nothing.
            return entry.token_dead()
        return bool(backup) and entry.token_dead(
            stored_fp=oauth.credential_fingerprint(backup)
        )

    def _plans_after_fetch(
        self,
        records: dict[str, FetchRecord],
        pre: dict[str, UsageEntry],
        info_by_num: dict[str, tuple],
    ) -> dict[str, tuple[float | None, float | None]]:
        """Build successful-fetch cadence updates for atomic outcome commit.

        Failures are paced by the store's backoff and keep their past-due plan
        for when the backoff lifts.
        """
        now = self._usage_store.clock()
        threshold, models = self._poll_policy_inputs()
        plans: dict[str, tuple[float | None, float | None]] = {}
        for num, rec in records.items():
            if rec.sentinel is not None or rec.error is not None:
                continue
            before = pre.get(num)
            recent_429 = before is not None and before.recent_429(now)
            plans[num] = poll_policy.plan_after_fetch(
                prev_interval_s=before.poll_interval_s if before else None,
                prev_usage=before.last_good if before else None,
                new_usage=rec.usage,
                is_active=bool(info_by_num[num][4]),
                threshold=threshold,
                models=models,
                recent_429=recent_429,
                now=now,
            )
        return plans

    def _replan_new_active(self, number: str, email: str, org_uuid: str) -> None:
        """Pull the just-activated account's poll plan to the active floor.

        Its stored plan was computed while it was an idle candidate and may
        wait up to CANDIDATE_MAX_INTERVAL_S — too slow for the account whose
        usage is about to move. The deadline anchors on the last measurement
        (an already-old one comes due immediately, a never-measured account
        is left plan-less so nothing blocks its first fetch), and the next
        poll is only ever pulled earlier, never pushed later. Best-effort by
        contract: the switch this rides on has already committed, so a cache
        hiccup here must not surface as a switch failure."""
        try:
            identities = {number: (email, org_uuid or "")}
            now = self._usage_store.clock()
            # No models needed: only fetched_at/next_poll_at is read here.
            entry = self._usage_store.entries(identities).get(number)
            if entry is None or entry.fetched_at is None:
                return
            next_poll = max(now, entry.fetched_at + poll_policy.MIN_INTERVAL_S)
            if entry.next_poll_at is not None and entry.next_poll_at <= next_poll:
                return
            self._usage_store.set_poll_plan(
                {number: (next_poll, poll_policy.MIN_INTERVAL_S)}, identities
            )
        except Exception as e:
            self._logger.warning(
                f"Post-switch poll re-plan failed (switch itself succeeded): {e}"
            )

    def _usage_by_account(self) -> dict[str, dict | str | None]:
        """Map account number → decision-grade usage value for managed accounts."""
        accounts_info = self._build_accounts_info()
        entries = self._collect_usage_entries(accounts_info)
        return {num: entry.decision_value() for num, entry in entries.items()}

    def _warn_inert_models(
        self,
        usage: dict,
        models: tuple[str, ...],
        json_output: bool,
        warnings: list[str],
    ) -> None:
        """One-shot typo guard for --model on the manual strategies.

        A configured name that no account reports gates nothing while looking
        active. Only claimed when every account's usage is readable (an
        unreadable account could be the one carrying the window)."""
        wanted = {m.lower(): m for m in models if m.lower() != "all"}
        if not wanted or not usage:
            return
        if any(not isinstance(v, dict) for v in usage.values()):
            return
        seen = {
            s["name"].lower()
            for v in usage.values()
            for s in (v.get("scoped") or [])
            if isinstance(s, dict) and isinstance(s.get("name"), str)
        }
        missing = [name for low, name in wanted.items() if low not in seen]
        if not missing:
            return
        msg = (
            f"model(s) {', '.join(missing)} match no account's usage windows "
            "(typo?)"
        )
        if json_output:
            warnings.append(msg)
        else:
            warning(msg)

    def _duplicate_account_warnings(
        self, accounts_info: list[tuple[int, str, str, str, bool, str, str]]
    ) -> list[str]:
        """Slots that provably authenticate as the same account.

        Impossible by construction, so a collision means one slot's credential
        was overwritten with another's (issue #117's end state) or the same
        account was registered twice. Two offline signals:

        - identical credential fingerprint (same refresh-token lineage or
          identical raw token) across two slots;
        - the same non-empty ``uuid`` + org recorded for two slots (empty
          uuids — add-token placeholders — never match each other).

        Limitation: two *different generations* of the same account (the
        poisoned end state a pre-guard switch could produce) carry different
        fingerprints and untouched sequence.json identities, so they are not
        offline-detectable here — ``_lockstep_usage_warnings`` covers that
        case heuristically. The switch-time guard prevents new occurrences
        whenever the identity oracle answers.
        """
        data = self._get_sequence_data() or {}
        by_fp: dict[str, str] = {}
        by_identity: dict[tuple[str, str], str] = {}
        out: list[str] = []
        for num, email, _org_name, org_uuid, _is_active, creds, _alias in accounts_info:
            snum = str(num)
            fp = oauth.credential_fingerprint(creds) if creds else None
            if fp:
                other = by_fp.get(fp)
                if other:
                    out.append(
                        f"Account-{other} and Account-{snum} hold the same "
                        f"credential ({email}) — one slot's backup was "
                        "overwritten. Log in with the missing account and "
                        "re-add it: openswap add --slot N"
                    )
                else:
                    by_fp[fp] = snum
            uuid = (data.get("accounts", {}).get(snum, {}).get("uuid") or "").strip()
            if uuid:
                key = (uuid, org_uuid or "")
                other = by_identity.get(key)
                if other and other != snum:
                    out.append(
                        f"Account-{other} and Account-{snum} both authenticate "
                        f"as {email} — remove or re-login one of them."
                    )
                elif not other:
                    by_identity[key] = snum
        return out

    def _lockstep_usage_warnings(
        self,
        accounts_info: list[tuple[int, str, str, str, bool, str, str]],
        entries: dict[str, UsageEntry],
    ) -> list[str]:
        """Heuristic: slots whose usage moves in perfect lockstep.

        Two different *generations* of the same account (the poisoned end
        state a pre-guard switch could produce — issue #117) carry different
        fingerprints and untouched sequence.json identities, so
        ``_duplicate_account_warnings`` cannot see them. But both tokens
        report the same account's usage: identical 5h *and* 7d percentages
        with identical reset timestamps — the exact signal the issue's
        reporter had to reverse-engineer by hand, automated here from data
        ``list`` already fetched.

        Heuristic, not proof: it goes quiet once the older generation dies
        and stops producing comparable usage, and only rows where both
        windows carry a non-null ``resets_at`` are compared (two idle
        accounts at 0% with nothing scheduled are indistinguishable, never
        flagged; API-key slots have sentinel usage and never reach the
        comparison). Known benign false-positive source until PR #119 lands:
        a session profile that drifted to another account makes its slot
        report that account's usage — same lockstep signature, different
        cause.
        """
        seen: dict[tuple, str] = {}
        out: list[str] = []
        for num, _email, _org_name, _org_uuid, _is_active, _creds, _alias in accounts_info:
            snum = str(num)
            entry = entries.get(snum)
            usage = entry.decision_value() if entry else None
            if not isinstance(usage, dict):
                continue
            h5 = usage.get("five_hour")
            d7 = usage.get("seven_day")
            if not isinstance(h5, dict) or not isinstance(d7, dict):
                continue
            key = (
                h5.get("pct"), h5.get("resets_at"),
                d7.get("pct"), d7.get("resets_at"),
            )
            if key[1] is None or key[3] is None or key[0] is None or key[2] is None:
                continue
            other = seen.get(key)
            if other:
                out.append(
                    f"Account-{other} and Account-{snum} report identical "
                    "usage and reset times — they may be the same account "
                    "(issue #117). If it persists, log in with the missing "
                    "account and re-add it: openswap add --slot N"
                )
            else:
                seen[key] = snum
        return out

    def _build_list_payload(
        self,
        accounts_info: list[tuple[int, str, str, str, bool, str, str]],
        entries: dict[str, UsageEntry],
    ) -> dict:
        """Build the ``--list --json`` payload from gathered account + usage data."""
        active_num: int | None = None
        accounts = []
        seq_data = self._get_sequence_data() or {}
        for num, email, org_name, org_uuid, is_active, _, alias in accounts_info:
            if is_active:
                active_num = num
            entry = entries[str(num)]
            # JSON carries the decision-grade value: last-good only while it is
            # recent enough to act on (≤ STALE_OK_S), else unavailable. Showing
            # older measurements is a human-display affordance only — scripts
            # keying on usageStatus == "ok" must not act on arbitrarily old data.
            accounts.append(
                account_row(
                    num, email, org_name, org_uuid, is_active,
                    entry.decision_value(),
                    usage_fetched_at=entry.fetched_at,
                    usage_age_s=entry.age_s,
                    last_good_usage=entry.last_good,
                    alias=alias,
                    disabled=self._disabled_from_data(seq_data, str(num)),
                )
            )
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "activeAccountNumber": active_num,
            "accounts": accounts,
        }
        # Additive fields (absent when clean) — never printed warnings; the
        # JSON contract keeps stdout a single machine-readable object.
        dup_warnings = self._duplicate_account_warnings(accounts_info)
        if dup_warnings:
            payload["duplicateAccountWarnings"] = dup_warnings
        lockstep_warnings = self._lockstep_usage_warnings(accounts_info, entries)
        if lockstep_warnings:
            payload["lockstepUsageWarnings"] = lockstep_warnings
        unclaimed = self._store._list_unclaimed_credentials()
        if unclaimed:
            payload["unclaimedCredentials"] = sorted(unclaimed)
        return payload

    def list_accounts(
        self,
        show_token_status: bool = False,
        json_output: bool = False,
        fetch: set[str] | None = None,
    ) -> dict | None:
        """List all managed accounts.

        In ``json_output`` mode, returns the schema-v1 payload (printing nothing)
        for the CLI to serialize; otherwise prints the human view and returns None.

        ``fetch`` restricts which accounts *may* be fetched this pass (an
        adaptive set); ``None`` — the CLI default — leaves every stale
        account eligible.
        """
        if not self.sequence_file.exists():
            # JSON mode must never prompt — emit an empty list instead of the
            # interactive first-run setup.
            if json_output:
                return {
                    "schemaVersion": SCHEMA_VERSION,
                    "activeAccountNumber": None,
                    "accounts": [],
                }
            print(dimmed("No accounts are managed yet."))
            self._first_run_setup()
            return None

        accounts_info = self._build_accounts_info()
        entries = self._collect_usage_entries(accounts_info, fetch=fetch)

        if json_output:
            return self._build_list_payload(accounts_info, entries)

        seq_data = self._get_sequence_data() or {}
        print(bolded("Accounts:"))
        for i, (num, email, org_name, org_uuid, is_active, _, alias) in enumerate(accounts_info):
            tag = self._get_display_tag(email, org_name, org_uuid)
            label = f"{accent(alias)} ({email})" if alias else email
            markers = ""
            if is_active:
                markers += f" {bold_accent('(active)')}"
            if self._disabled_from_data(seq_data, str(num)):
                markers += f" {muted('(disabled)')}"
            print(f"  {num}: {label} {muted(f'[{tag}]')}{markers}")
            for line in _usage_entry_lines(entries[str(num)]):
                print(f"     {line}")

            if show_token_status:
                for line in self._token_status_lines(accounts_info[i]):
                    print(f"     {dimmed('•')} {muted(line)}")
            if i < len(accounts_info) - 1:
                print()

        # Safety copies (unclaimed credentials) are deliberately NOT surfaced
        # here: users can't act on them (recovery is always /login + openswap
        # add), and with no GC a one-time event would nag forever. They stay
        # in the JSON payload and logs for diagnostics.
        dup_warnings = self._duplicate_account_warnings(accounts_info)
        lockstep_warnings = self._lockstep_usage_warnings(accounts_info, entries)
        if dup_warnings or lockstep_warnings:
            print()
            for msg in dup_warnings:
                warning(msg)
            for msg in lockstep_warnings:
                warning(msg)

        # Running instances
        try:
            sessions, ide_instances = get_running_instances()

            if sessions or ide_instances:
                # Group by (label, folder) to avoid repetitive lines
                groups: dict[tuple[str, str], dict[str, int]] = {}
                for session in sessions:
                    label = entrypoint_label(session.entrypoint)
                    cwd = abbreviate_path(session.cwd)
                    key = (label, cwd)
                    counts = groups.setdefault(key, {"sessions": 0, "ide": 0})
                    counts["sessions"] += 1
                for ide in ide_instances:
                    name = ide_short_name(ide.ide_name)
                    for folder in ide.workspace_folders:
                        key = (name, abbreviate_path(folder))
                        counts = groups.setdefault(key, {"sessions": 0, "ide": 0})
                        counts["ide"] += 1

                print()
                print(bolded("Running instances:"))
                for (label, cwd), counts in groups.items():
                    parts = []
                    s = counts["sessions"]
                    if s:
                        parts.append(f"{s} session{'s' if s > 1 else ''}")
                    if counts["ide"]:
                        parts.append("IDE")
                    print(f"  {dimmed('●')} {muted(label)}   {muted(cwd)}  {dimmed(f'({", ".join(parts)})')}")
        except Exception:
            self._logger.debug("Failed to detect running instances", exc_info=True)

    def _active_account_usage(
        self, account_num: str, current_email: str, org_uuid: str
    ) -> UsageEntry:
        """Store-backed usage entry for just the active account.

        Builds a single-account info row instead of the full accounts list
        (``--status`` touches one slot) and runs it through the shared
        collector, so freshness/backoff/claim gating and the shared
        ``cache/usage.json`` table behave exactly as in ``--list``.
        """
        creds = self._active_usage_credentials(
            str(account_num), current_email, load_backup=True
        )
        info = (int(account_num), current_email, "", org_uuid or "", True, creds, "")
        return self._collect_usage_entries([info])[str(account_num)]

    def _build_status_payload(self) -> dict:
        """Build the ``--status --json`` payload (no active / unmanaged / managed)."""
        identity = self._get_current_account()
        if identity is None:
            return {"schemaVersion": SCHEMA_VERSION, "active": None}
        current_email, current_org_uuid = identity

        data = self._get_sequence_data_migrated()
        if not data:
            return {
                "schemaVersion": SCHEMA_VERSION,
                "active": {"email": current_email, "managed": False},
            }

        account_num = self._find_account_slot(data, current_email, current_org_uuid)
        if not account_num:
            return {
                "schemaVersion": SCHEMA_VERSION,
                "active": {"email": current_email, "managed": False},
            }

        acct = data["accounts"][account_num]
        org_name = acct.get("organizationName", "") or ""
        org_uuid = acct.get("organizationUuid", "") or ""
        alias = acct.get("alias", "") or ""
        entry = self._active_account_usage(account_num, current_email, org_uuid)
        # Decision-grade projection, same rule as the --list payload: stale
        # beyond STALE_OK_S reports unavailable, not "ok" with old numbers.
        status, usage = usage_fields(entry.decision_value(), entry.fetched_at)
        active: dict = {
            "number": int(account_num),
            "email": current_email,
            "organizationName": org_name,
            "organizationUuid": org_uuid,
            "isOrganization": bool(org_uuid),
            "managed": True,
            "usageStatus": status,
            "usage": usage,
        }
        if alias:
            active["alias"] = alias
        if usage is not None:
            active.update(usage_freshness_fields(entry.fetched_at, entry.age_s))
        else:
            active.update(
                last_good_usage_fields(
                    entry.last_good, entry.fetched_at, entry.age_s
                )
            )
        return {
            "schemaVersion": SCHEMA_VERSION,
            "active": active,
            "totalManagedAccounts": len(data.get("accounts", {})),
        }

    def status(self, json_output: bool = False) -> dict | None:
        """Display current account status (or return the schema-v1 payload)."""
        if json_output:
            return self._build_status_payload()

        identity = self._get_current_account()
        if identity is None:
            print(f"{bolded('Status:')} {dimmed('No active Claude account')}")
            return None
        current_email, current_org_uuid = identity

        data = self._get_sequence_data_migrated()
        if not data:
            print(f"{bolded('Status:')} {current_email} {dimmed('(not managed)')}")
            return None

        account_num = self._find_account_slot(data, current_email, current_org_uuid)
        org_name = ""
        if account_num is not None:
            org_name = data["accounts"][account_num].get("organizationName", "") or ""

        if account_num:
            tag = self._get_display_tag(current_email, org_name, current_org_uuid)
            total = len(data.get("accounts", {}))
            print(
                f"{bolded('Status:')} {accent(f'Account-{account_num}')} "
                f"({current_email} {muted(f'[{tag}]')})"
            )
            print(f"  {dimmed(f'Total managed accounts: {total}')}")
            entry = self._active_account_usage(
                account_num, current_email, current_org_uuid
            )
            for line in _usage_entry_lines(entry):
                print(f"  {line}")
        else:
            print(f"{bolded('Status:')} {current_email} {dimmed('(not managed)')}")
        return None
