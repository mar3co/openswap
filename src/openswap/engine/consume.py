"""OpenSwap account engine: One-time refresh-token consume: CAS on fingerprint, unclaimed stash on persist failure."""

from __future__ import annotations

from contextlib import nullcontext

from openswap.engine.notes import *  # noqa: F403
from openswap.worker.leases import AccountLeaseError, AccountLeaseStore

class ConsumeMixin:
    """One-time refresh-token consume: CAS on fingerprint, unclaimed stash on persist failure."""

    def consume_backup_grant(
        self, account_num: str, email: str, snapshot: str
    ) -> "oauth.RefreshOutcome":
        """The gate through which a backup refresh token is consumed.

        Not the only site that POSTs one: ``_fetch_active_usage``'s recovery
        branch can POST the slot's backup grant too, when the live bytes moved
        or were cleared. It is not an escape — it takes this same per-slot
        *consume lock*, in this same order, and its own comment at that site
        records why. What is single here is the SERIALIZATION, not the call
        site, and saying "the single place" instead sends the next reader
        looking for a violation rather than for the second lock holder.

        A refresh token is one-time-use, so the POST must consume the
        provably-freshest copy of the slot's grant — never a caller's
        snapshot, which may be a superseded generation. The whole sequence
        runs under the per-slot consume lock (re-read → POST → CAS) so two
        consumers cannot POST the same grant. It also holds the provider
        mutation guard through POST and persistence so a worker lease cannot
        begin with the generation whose grant was just consumed.

        The body below adopts any stashed successor, re-reads the current
        generation, POSTs it, then CASes on the refresh-token fingerprint and
        either persists or stashes the successor. A consumed generation is
        never discarded — a stash is adopted by the next pass — and the gate
        never raises after the grant is consumed, since callers run in the
        never-raises collect pass.

        Returns a ``RefreshOutcome``: ``credentials`` is the slot's
        now-current credential on success (ours, or a racing writer's adopted
        newer lineage); ``error`` carries the refresh failure unchanged. Every
        outcome carries ``consumed_fp`` — strike binding must follow the bytes
        the gate actually POSTed, which may differ from the caller's snapshot.

        The caller must NOT hold ``self.lock_file`` (non-reentrant).
        """
        # Store-resolution parity: CC ≥2.1.220 honors
        # CLAUDE_SECURESTORAGE_CONFIG_DIR for its credential store. openswap
        # mirrors that resolution on the CAPTURE path (#205 —
        # `_read_capture_credentials` reads the store CC would read), but
        # the consume and switch paths still resolve the DEFAULT store.
        # Consuming a grant read from the default store while CC
        # reads/writes the redirected one is the stale-copy failure class by
        # construction — refuse (transient, so nothing strikes) rather than
        # operate on a store CC left behind.
        if os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR"):
            self._logger.warning(
                "CLAUDE_SECURESTORAGE_CONFIG_DIR is set; openswap mirrors it "
                "when capturing a credential but not when consuming one, "
                "so refusing to consume account %s's refresh token "
                "(unset the variable or run from a normal shell).",
                account_num,
            )
            # Distinct kind: deterministic and self-inflicted (an env var),
            # so it must SURFACE — a transient would fall through to a
            # guaranteed-401 usage call every pass and read as generic
            # network trouble forever.
            return oauth.RefreshOutcome(None, "store-unmirrored")

        # Consume serialization: one in-flight consume per slot, held across
        # re-read → POST → CAS. The provider mutation guard also spans this
        # bounded refresh request so kickoff cannot acquire a durable lease
        # between consuming a one-time grant and persisting its successor.
        # The consume lock serializes refresh consumers; provider lock order
        # is consume lock then provider lock, and no lease path takes the
        # consume lock.
        consume_lock = FileLock(
            self.credentials_dir / f".consume-{account_num}.lock"
        )
        if not consume_lock.acquire():
            self._logger.info(
                "Another consume is in flight for account %s; deferring to "
                "the next pass.", account_num,
            )
            # Distinct from "transient": nothing failed and nothing is remote.
            # Another gate holds the slot and will finish; this pass simply
            # yields. Reported as its own kind so the tick error does not
            # blame the network for local serialization working as designed.
            return oauth.RefreshOutcome(None, "consume-busy")
        try:
            try:
                with AccountLeaseStore(
                    self.backup_dir, "claude"
                ).mutation_guard() as guard:
                    guard.assert_available()
                    return self._consume_backup_grant_locked(
                        account_num, email, snapshot
                    )
            except AccountLeaseError:
                # A durable active, uncertain, or unreadable lease prevents
                # refresh-token use. Nothing was POSTed, so this is a
                # deferred pass rather than a failed grant.
                self._logger.info(
                    "A worker lease blocks backup refresh for account %s; "
                    "deferring to the next pass.", account_num,
                )
                return oauth.RefreshOutcome(None, "consume-busy")
            except LockError:
                self._logger.info(
                    "Provider mutation lock is busy; deferring backup "
                    "refresh for account %s.", account_num,
                )
                return oauth.RefreshOutcome(None, "consume-busy")
        finally:
            consume_lock.release()

    def _consume_backup_grant_locked(
        self, account_num: str, email: str, snapshot: str
    ) -> "oauth.RefreshOutcome":
        """Body of ``consume_backup_grant``; caller holds both guards."""
        from openswap.session import (
            is_session_stale,
            read_session_credentials,
            session_dir_for,
            session_identity_drifted,
        )

        try:
            # ``consume_backup_grant`` holds the provider mutation guard for
            # this entire helper, including the refresh request and CAS.
            with nullcontext():
                current, unreadable = self._read_account_credentials_ex(
                    account_num, email
                )
                if unreadable:
                    # The backup may exist but cannot be seen (macOS
                    # keychain locked/denied): the snapshot is exactly the
                    # possibly-superseded copy this gate exists to never
                    # consume. Defer; nothing consumed.
                    self._logger.info(
                        "Backup for account %s unreadable (keychain); "
                        "deferring the refresh.", account_num,
                    )
                    return oauth.RefreshOutcome(None, "transient")
                # Adopt a stashed successor from a prior gate whose persist
                # failed: if the store still holds the generation that
                # successor superseded, writing it back IS the pending
                # persist — and saves consuming a grant at all.
                try:
                    adopted_creds = self._adopt_stashed_successor(
                        account_num, email, current
                    )
                except CredentialReadError:
                    # The slot's only successor is unreadable. Deferring is
                    # right -- the bytes are the SOLE copy of a generation
                    # this slot already consumed, and nothing on disk tells
                    # "locked for a minute" from "locked forever", so
                    # retiring on a strike count or an age bound would
                    # destroy a live refresh token every time the cause was
                    # merely slow. What must not stay is the LABEL: the
                    # generic handler below degrades this to "transient",
                    # which the tick renders as "could not freshen any
                    # candidate (network?)" -- sending the operator to check
                    # a connection that is fine, forever, on a condition
                    # only they can clear (unlock the Keychain, fix the
                    # mode, remount the volume) or drop
                    # (`openswap unclaimed --purge`).
                    self._logger.info(
                        "Account %s's stashed successor is unreadable; "
                        "deferring the refresh.", account_num, exc_info=True,
                    )
                    return oauth.RefreshOutcome(None, "stash-unreadable")
                if adopted_creds is not None:
                    current = adopted_creds
                if not current:
                    # ABSENT, not unreadable (that branch returned above): the
                    # slot was removed between the caller's read and this
                    # locked re-read. Falling back to the caller's snapshot
                    # spends a grant for an account the user just deleted and
                    # stashes a successor keyed to a generation no slot holds
                    # — `_adopt_stashed_successor` returns early on an empty
                    # store fingerprint, so nothing can ever adopt it. The CAS
                    # branch below already refuses to WRITE that successor
                    # (`consume-gate-slot-removed`); this is the same rule one
                    # step earlier, before the grant is spent rather than
                    # after. Every production caller reads its snapshot from
                    # the backup store, so absent here really does mean gone.
                    self._logger.info(
                        "Account %s's stored credential is gone; deferring "
                        "the refresh rather than consuming a grant for a "
                        "slot that no longer exists.", account_num,
                    )
                    return oauth.RefreshOutcome(None, "transient")
                refresh_input = current
                input_oauth = oauth.extract_oauth_data(refresh_input)
                # Session-profile precedence: only when no live session owns
                # the profile (a live claude rotates its own tokens — #97's
                # rule) and the profile identity — org included: two slots
                # may share an email across orgs — still matches the slot.
                org_uuid = (
                    (self._get_sequence_data() or {})
                    .get("accounts", {})
                    .get(account_num, {})
                    .get("organizationUuid", "")
                    or ""
                )
                if not self._live_session_pids(account_num, email):
                    sdir = session_dir_for(self.backup_dir, account_num, email)
                    profile = read_session_credentials(sdir)
                    if (
                        profile
                        # A marked profile's credentials are presumed stale
                        # (backup changed under the live session — e.g. a
                        # deliberate re-add/import): never let it supersede
                        # the backup it is presumed stale against.
                        and not is_session_stale(sdir)
                        and not session_identity_drifted(sdir, email, org_uuid)
                    ):
                        prof_oauth = oauth.extract_oauth_data(profile)
                        cur_exp = (input_oauth or {}).get("expiresAt") or 0
                        prof_exp = (prof_oauth or {}).get("expiresAt") or 0
                        if (
                            prof_oauth
                            and prof_oauth.get("accessToken")
                            and prof_oauth.get("refreshToken")
                            and oauth.credential_fingerprint(profile)
                            != oauth.credential_fingerprint(refresh_input)
                            and prof_exp > cur_exp
                        ):
                            # The profile holds the newer generation: the
                            # backup rt is already consumed. Resync so the
                            # slot's stored credential is the live lineage,
                            # then consume THAT.
                            self._write_account_credentials(
                                account_num, email, profile
                            )
                            refresh_input = profile
                            input_oauth = prof_oauth
                consumed_fp = oauth.credential_fingerprint(refresh_input)
        except LockError:
            # Nothing consumed yet — a holder (switch, collector, CC) owns
            # the slot; defer cleanly rather than raise through callers
            # that promise never to (the collect pass thread-pools us).
            # "Nothing consumed" is about the POST, not the store -- see
            # the generic handler below.
            self._logger.info(
                "Slot lock held elsewhere; deferring account %s's backup "
                "refresh to the next pass.", account_num,
            )
            return oauth.RefreshOutcome(None, "transient")
        except Exception:
            # No POST has been issued yet, so no grant of ours is
            # outstanding — degrade to transient instead of raising through
            # the never-raises collect pass.
            #
            # "Nothing consumed" is about the POST, NOT about the store. The
            # resync and the adoption both WRITE before this point, and an
            # earlier version of this comment claimed they raise first; they
            # do not.
            #
            # So this handler IS reachable with the slot already advanced:
            # `_adopt_stashed_successor` makes everything past its own store
            # write non-fatal, but the adopt is not the last thing in this
            # `try` — `_get_sequence_data` reads with `strict=True` and comes
            # AFTER it, so a torn `sequence.json` raises to here over a slot
            # that adoption already freshened.
            #
            # `transient` is still the right answer there, and for the reason
            # the first line gives rather than an unadvanced slot: no grant of
            # ours is outstanding, so deferring costs a pass and spends
            # nothing. The cost of the advanced case is only that the next
            # pass re-reads a slot that is already fresh.
            self._logger.warning(
                "Pre-consume window failed for account %s; deferring.",
                account_num, exc_info=True,
            )
            return oauth.RefreshOutcome(None, "transient")

        input_oauth = input_oauth or {}
        snap_at = (oauth.extract_oauth_data(snapshot) or {}).get("accessToken")
        if (
            input_oauth.get("accessToken")
            and snap_at
            and input_oauth.get("accessToken") != snap_at
            and not oauth.is_oauth_token_expired(input_oauth.get("expiresAt"))
        ):
            # The world already moved past the caller's snapshot AND the
            # current generation is fresh: the refresh the caller wanted
            # has effectively happened (a racing gate's rotation, an
            # adopted stash, a live profile's newer family). Adopt it —
            # consuming another grant on top burns a generation for
            # nothing. When the re-read equals the snapshot (the 401-retry
            # shape: the server just rejected these exact bytes), this
            # never fires and the POST proceeds.
            return oauth.RefreshOutcome(refresh_input, None, None, consumed_fp)

        result = oauth.try_refresh_oauth_credentials(refresh_input)
        if result.error is not None or not result.credentials:
            # Strike binding must follow the POSTed bytes: the gate may have
            # substituted a locked re-read or the session profile for the
            # caller's snapshot, and failures are the only outcomes that
            # strike.
            return dataclasses.replace(result, consumed_fp=consumed_fp)

        stashed_reason = ""

        def stash_successor(reason: str, note: str) -> None:
            # A consumed generation is never discarded: park the successor
            # where the next gate pass adopts it (see
            # ``_adopt_stashed_successor``; ``consumedFp`` is the adoption
            # key — the generation this successor superseded).
            self._store._write_unclaimed_credential(
                result.credentials,
                {
                    "reason": reason,
                    "configSlot": account_num,
                    "consumedFp": consumed_fp,
                    "fingerprint": oauth.credential_fingerprint(
                        result.credentials
                    ),
                },
            )
            nonlocal stashed_reason
            stashed_reason = reason
            self._logger.warning(note, account_num)

        outcome_creds = result.credentials
        try:
            try:
                # The outer provider mutation guard remains held here.
                with nullcontext():
                    store_now, store_unreadable = (
                        self._read_account_credentials_ex(account_num, email)
                    )
                    if store_unreadable:
                        # The Keychain locked during the POST (macOS screen
                        # lock is ~1s away at any moment). The CAS cannot be
                        # evaluated: writing back could clobber a racing
                        # writer, and the plain reader's `""` would otherwise
                        # be read as "the slot was emptied", whose reason is
                        # deliberately NOT demoting. The grant IS spent and
                        # the slot may still hold the generation that spent
                        # it, so this must stash AND demote.
                        stash_successor(
                            "consume-gate-store-unreadable",
                            "Account %s's stored credential was unreadable "
                            "(keychain) after a refresh POST; successor "
                            "stashed, nothing rewritten.",
                        )
                    elif not store_now:
                        # The slot was emptied mid-POST (remove-account):
                        # writing the successor back would resurrect
                        # credentials the user just deleted. Park it
                        # instead.
                        stash_successor(
                            "consume-gate-slot-removed",
                            "Account %s's stored credential disappeared "
                            "during a refresh POST; successor stashed, "
                            "nothing rewritten.",
                        )
                    elif (
                        oauth.credential_fingerprint(store_now) != consumed_fp
                    ):
                        # A writer replaced the lineage while our POST was in
                        # flight: stash our successor, adopt the store's
                        # newer credential.
                        stash_successor(
                            "consume-gate-cas-conflict",
                            "Backup lineage for account %s moved during a "
                            "refresh POST; successor stashed, adopting the "
                            "newer store credential.",
                        )
                        outcome_creds = store_now
                    else:
                        self._write_account_credentials(
                            account_num, email, result.credentials
                        )
            except LockError:
                # The grant IS consumed — the successor must survive even
                # though the persist lock is unavailable. The token works;
                # the next gate pass adopts from the stash.
                stash_successor(
                    "consume-gate-persist-lock-failed",
                    "Slot lock unavailable after consuming account %s's "
                    "grant; successor stashed for the next pass.",
                )
        except Exception:
            # The grant IS consumed and callers (the thread-pooled collect
            # pass) promise never to raise: a persist OR stash failure must
            # not escape. Last resort is the stash; if that also fails
            # (same-dir I/O error, e.g. disk full), the successor survives
            # only in this return value — say so loudly.
            self._logger.warning(
                "Persisting account %s's refreshed credential failed; "
                "stashing instead.", account_num, exc_info=True,
            )
            try:
                stash_successor(
                    "consume-gate-persist-failed",
                    "Persist failed after consuming account %s's grant; "
                    "successor stashed for the next pass.",
                )
            except Exception:
                # Both the persist and the stash failed. stash_successor sets
                # stashed_reason after its write, so a raising write left it
                # empty and the guard below reported success on a spent grant
                # with nothing stashed.
                stashed_reason = "consume-gate-unpersisted"
                self._logger.error(
                    "Account %s's consumed successor could not be persisted "
                    "or stashed — it survives only for this pass. Fix the "
                    "storage failure, then re-login and `openswap add` if the "
                    "slot strikes.", account_num, exc_info=True,
                )
        if stashed_reason in _DEMOTING_STASH_REASONS:
            # The successor is parked, not persisted: the slot still holds the
            # generation whose grant we just spent. Callers read `error is
            # None` as "the slot is freshened and safe to activate" — after a
            # failed persist it is the opposite, and activating it installs an
            # expired access token that can never refresh, so Claude Code logs
            # the account out. Report transient so the caller defers; the next
            # pass adopts the stash and succeeds normally. The credentials
            # still ride along, so a caller that only needs a live token for
            # THIS request keeps working.
            #
            # Two stash reasons are excluded, for opposite reasons.
            #
            # A REMOVED slot: there is nothing left to activate or retry, so
            # deferring would only turn a completed user action into a
            # recurring error.
            #
            # A CAS CONFLICT: the slot is FRESHENED, which is the condition
            # this demotion exists to deny. A racing writer won and wrote a
            # newer valid lineage; we adopted it and it is what the caller
            # asked for. Reporting it as an error made `_freshen_target` skip
            # a healthy candidate and the tick emit "could not freshen any
            # candidate (network?)" on every multi-surface race — the exact
            # contention this gate was built for, turned into a false alarm.
            return oauth.RefreshOutcome(
                outcome_creds, "transient", result.token_account, consumed_fp,
                # `consume-gate-unpersisted` is set precisely WHEN the stash
                # write raised, so it is the one demoting reason that did not
                # park anything. Every other one wrote the entry first.
                stashed=stashed_reason != "consume-gate-unpersisted",
            )
        return oauth.RefreshOutcome(
            outcome_creds, None, result.token_account, consumed_fp
        )

    def _retire_stash_entry(self, entry_id: str, account_num: str) -> None:
        """Drop one stash entry as housekeeping. Never fatal.

        Every call site is inside the adopt scan, and the one that matters
        runs AFTER ``_write_account_credentials`` has already advanced the
        slot. ``_remove_unclaimed_credential`` can raise there -- its manifest
        rewrite ends in ``atomic_write_json`` (``OSError`` on a full disk or a
        read-only mount) under a lock that can time out (``LockError``) -- and
        a raise escaping a COMPLETED adoption is read by
        ``_consume_backup_grant_locked`` as a failed refresh, so the caller
        re-POSTs a generation this pass already consumed.

        The two costs differ by an order of magnitude. Losing a retire leaves
        one stale row, which the next pass retries and `openswap unclaimed
        --purge` drops by hand. Losing the adoption discards a live credential
        already written to the store. So: log and continue.
        """
        try:
            self._store._remove_unclaimed_credential(entry_id)
        except Exception:
            self._logger.warning(
                "Could not retire account %s's stash entry %s; leaving it for "
                "the next pass (`openswap unclaimed --purge` drops it by hand).",
                account_num, entry_id, exc_info=True,
            )

    def _adopt_stashed_successor(
        self, account_num: str, email: str, current: str
    ) -> str | None:
        """Complete a prior gate's failed persist from the unclaimed stash.

        A stash entry records ``consumedFp`` — the generation its credential
        superseded. When the slot still stores exactly that generation, the
        stored rt is already consumed and the stash holds its live
        successor: write it back (the pending persist) and drop the entry.
        Returns the adopted credentials, or None when nothing applies.
        Caller holds the slot FileLock.
        """
        cur_fp = oauth.credential_fingerprint(current)
        if not cur_fp:
            return None
        # A row that is merely unreadable THIS instant (locked keychain,
        # transient EIO) must not abort the scan before a later, readable
        # sibling on the same generation is tried (repeated persist-failures
        # can stash more than one row against the same consumedFp). Remember
        # it and keep scanning; only defer via CredentialReadError once no
        # row adopted.
        deferred_entry_id: str | None = None
        manifest, manifest_verdict = self._store._read_stash_manifest_ex()
        if manifest_verdict == "unreadable" or (
            manifest_verdict == "corrupt"
            and self._store._stash_entry_files_exist()
        ):
            # Not "nothing stashed": the rows cannot be established, and entry
            # bytes are at risk. Every row this scan would have read is the
            # sole record of a generation some pass already consumed, so an
            # empty scan makes the caller POST the slot's spent generation.
            #
            # That POST does not cost "one retry". The generation is spent by
            # construction, so it returns invalid_grant, and the gate returns
            # before any manifest write — nothing is set aside, nothing
            # self-heals, and at AUTH_DEAD_STRIKES=1 a live account is
            # quarantined while its successor sits orphaned on disk.
            #
            # CORRUPT with no entry files falls through instead: `{}` is then
            # not a guess about a pending successor, there provably is none,
            # and proceeding lets ``_write_stash_manifest`` set the bad file
            # aside — the only repair, and it only runs on a write.
            #
            # Fail-closed still has an exit: ``_list_unclaimed_credentials``
            # globs the entry files, so `openswap unclaimed` lists the orphans by
            # id and `--purge` drops them even with the manifest unreadable.
            raise CredentialReadError(
                f"the unclaimed manifest is {manifest_verdict} and stashed "
                f"entry files exist; deferring account {account_num}'s "
                "adoption rather than POSTing a generation a stashed "
                "successor may already have superseded (`openswap unclaimed` "
                "lists them, `--purge` drops one)"
            )
        for entry_id, meta in manifest.items():
            if meta.get("configSlot") != account_num:
                continue
            if meta.get("consumedFp") != cur_fp:
                if meta.get("reason") == "consume-gate-cas-conflict":
                    # A CAS-conflict entry can NEVER match: the conflict is by
                    # definition "the store moved off the generation we
                    # consumed", and the store only moves forward, so it never
                    # returns. Left alone these accumulate one file per
                    # conflict — the common outcome on a busy multi-surface
                    # setup — each indistinguishable from an entry still
                    # awaiting adoption. Retire it here, where the slot lock is
                    # already held and the current generation is in hand.
                    #
                    # Retiring is safe: the gate adopted the store's newer
                    # lineage in the same breath, so this successor branches
                    # off a generation that lineage already superseded. It is
                    # not the pending persist it looks like.
                    self._retire_stash_entry(entry_id, account_num)
                    self._logger.info(
                        "Retired account %s's CAS-conflict stash entry: its "
                        "generation was superseded by the writer that won the "
                        "race, so no pass can ever adopt it.", account_num,
                    )
                elif not any(self._store._read_unclaimed_credential(entry_id)):
                    # No bytes and no matching generation: nothing can ever
                    # adopt this row, and no other reason retires it. This is
                    # the state a FAILED retire leaves -- the bytes are
                    # unlinked before the manifest rewrite, so an OSError
                    # there orphans the row while the adoption that preceded
                    # it moved the slot off the generation it keys against.
                    #
                    # The READER, not `exists()`: `Path.exists()` swallows
                    # only ENOENT-shaped errors, so an EACCES/EIO would raise
                    # straight out of the scan and strand an adoptable sibling
                    # behind this row. `any(...)` is false only for
                    # ("", False) -- absent or corrupt. A merely UNREADABLE
                    # row is ("", True) and survives: its bytes may hold a
                    # real superseded token, so dropping it stays the
                    # operator's call (`openswap unclaimed --purge`).
                    self._retire_stash_entry(entry_id, account_num)
                    self._logger.info(
                        "Retired account %s's byte-less stash entry: its "
                        "credential is gone and its generation has passed, "
                        "so no pass could ever adopt it.", account_num,
                    )
                continue
            creds, unreadable = self._store._read_unclaimed_credential(entry_id)
            if unreadable:
                # This entry is the SOLE copy of a generation a prior gate
                # pass already consumed and could not persist. Falling
                # through here would make the caller POST the slot's
                # spent generation -- an unrecoverable invalid_grant on an
                # account whose live credential is sitting right here,
                # merely unreadable this instant. Remember it and keep
                # looking for a readable sibling on the same generation
                # before giving up.
                if deferred_entry_id is None:
                    deferred_entry_id = entry_id
                continue
            if not creds:
                # ABSENT (unlinked bytes, matching manifest row) or CORRUPT
                # (undecodable) -- either way the bytes are permanently gone,
                # not merely inaccessible right now, so nothing can ever
                # adopt this row. Retire it now: left alone it is rescanned
                # on every gate pass and leaks in --json's
                # unclaimedCredentials forever, the same accumulation the
                # CAS-conflict branch above already retires on sight.
                self._retire_stash_entry(entry_id, account_num)
                self._logger.info(
                    "Retired account %s's unreadable-bytes stash entry: its "
                    "generation is gone, so no pass could ever adopt it.",
                    account_num,
                )
                continue
            # The WRAPPER, not the store method plus a private repeat of its
            # tail: `_write_account_credentials` already contains the
            # invalidation and leaves STALE_MARKER when it cannot run, so it
            # cannot raise past its own store write. Open-coding the split
            # here made this one call site safe and left the other two — the
            # resync and the post-POST persist — carrying the defect.
            self._write_account_credentials(account_num, email, creds)
            # Housekeeping, and non-fatal for the same reason: the slot is
            # advanced, so a raise would report a failed refresh for a
            # credential the store holds. A stale row is retried next pass or
            # dropped with `openswap unclaimed --purge`.
            self._retire_stash_entry(entry_id, account_num)
            self._logger.info(
                "Adopted account %s's stashed successor (%s): the stored "
                "generation was already consumed by the gate pass that "
                "stashed it.", account_num, meta.get("reason", "unknown"),
            )
            return creds
        if deferred_entry_id is not None:
            # No row on this generation adopted; the deferred one is the
            # only copy of a generation this slot already consumed. Raise
            # into the caller's existing pre-consume exception handling,
            # which already degrades to "transient" and defers to the next
            # pass rather than discarding that generation.
            raise CredentialReadError(
                f"stash entry {deferred_entry_id} for account {account_num} "
                "is unreadable; deferring adoption rather than discarding "
                "its generation"
            )
        return None
