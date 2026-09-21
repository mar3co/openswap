"""OpenSwap account engine: Capture, activate, classify outgoing live bytes, shared MCP merge."""

from __future__ import annotations

from openswap.engine.notes import *  # noqa: F403

class SwitchMixin:
    """Capture, activate, classify outgoing live bytes, shared MCP merge."""

    def _prepare_credentials_for_activation(
        self, target_credentials: str, live_credentials: str | None
    ) -> str:
        """Compose the credential to activate from its two owners.

        The machine-shared OAuth integrations (the ``SHARED_CREDENTIAL_KEYS``
        allowlist, notably ``mcpOAuth``) are frozen in the slot at backup
        time and may hold rotated-out refresh tokens, while the live
        credential's copies are by definition the current generation — so
        for those keys the live credential wins, absence included. Every
        other field the destination slot stored travels with the slot:
        account-bound state such as ``trustedDeviceToken`` — and any field
        openswap does not recognize — must not leak across an account switch.

        When there is no live JSON credential object to take shared fields
        from (fresh machine, or a managed API key is active), the stored
        blob activates unchanged, exactly as before.
        """
        live_shared = shared_credential_fields(live_credentials)
        if live_shared is None:
            return target_credentials
        return merge_shared_credential_fields(target_credentials, live_shared)

    def live_session_pids_for(self, account_num: str, email: str) -> list[int]:
        """Public wrapper: PIDs of live isolated-profile Claude sessions for a slot."""
        return self._live_session_pids(account_num, email)

    def _reject_identity_drift_since_verify(
        self, verified: tuple[str, str, str]
    ) -> None:
        """Refuse when the active identity moved during the ownership check.

        ``add_account`` verifies a credential over the network and then reads
        ``.claude.json`` again for the bytes it stores. A ``/login`` landing in
        that window puts one account's identity on another's credential -- the
        same LABELLED-one/CONTAINS-another shape
        ``_reject_foreign_credential_capture`` exists to close, by a different
        door. BOTH write paths need this: ``slot=None`` on a registered account
        is the branch the menu bar and a bare ``--add-account`` take.
        """
        now = self._get_current_identity_triple()
        if now == verified:
            return
        raise ConfigError(
            f"The active account changed while {verified[0]} was being "
            f"verified (now {(now[0] if now else '') or 'unknown'}). Nothing "
            f"was changed. Re-run when no other login is in flight."
        )

    def _reject_credential_drift_since_verify(self, verified: str) -> None:
        """Refuse when the credential store rotated during the ownership check.

        The identity guard sees a ``/login`` because that moves
        ``oauthAccount``. A plain refresh of the SAME account does not: the
        identity is unchanged and only the credential moved. Storing the
        pre-refresh bytes hands the slot a generation the server has already
        retired, so the slot's next refresh gets ``invalid_grant``.

        ``credential_fingerprint`` is LINEAGE identity -- it hashes the refresh
        token, so an access-token-only rotation compares equal on purpose. A
        difference therefore means the lineage advanced, not merely that bytes
        changed.

        Unreadable is UNVERIFIABLE, not a refusal: this can only ever add a
        refusal, and a store that cannot be re-read is the fail-open case the
        ownership guard already treats that way.
        """
        try:
            now = self._read_capture_credentials()
        except Exception:  # noqa: BLE001 -- unreadable is unverifiable
            return
        if not now:
            return
        before = oauth.credential_fingerprint(verified)
        after = oauth.credential_fingerprint(now)
        if before is None or after is None or before == after:
            return
        raise ConfigError(
            "The stored credential rotated while it was being verified. "
            "Nothing was changed. Registering the pre-rotation generation "
            "would hand the slot a credential the server has already "
            "retired. Re-run when no refresh is in flight."
        )

    def _reject_foreign_credential_capture(
        self, creds: str, email: str, org_uuid: str, account_uuid: str
    ) -> str:
        """Guard for ``add_account``: the stored token must be THIS account's.

        ``add_account`` reads the IDENTITY from ``.claude.json``'s
        ``oauthAccount`` and the CREDENTIAL from the keychain/file store.
        Those are two different sources and nothing made them agree.

        Measured in the field: a session registered one account's address
        and the slot received a different account's token — an ssh session had
        ``.claude.json`` renamed to the new profile while the live keychain
        item still held the original account's credential. The slot ends up
        LABELLED one account and CONTAINING another, so every later switch to
        it logs the wrong user in and ``--status`` shows a name that is not
        whose token is stored. Nothing surfaces the disagreement.

        ``fetch_oauth_profile`` already answers exactly this ("whose token is
        this") and the autoswitch identity oracle already uses it. Compared
        UUID-FIRST, like ``_resolved_matches_slot_identity``: uuids are stable
        where an email can be recycled across accounts, so the EMAIL decides
        only when the slot carries no uuid to compare. The org is corroborated
        either way -- a uuid match under a disagreeing org is another account.

        An expired ACCESS token is UNRESOLVABLE here, not refreshed. A live
        refresh token would revive it, but spending that grant is a
        coordinated transition everywhere else in this class -- under the
        account FileLock and CC's credential locks, with the successor
        persisted to BOTH stores, because an accepted rotation retires its
        predecessor server-side. Refreshing here would strand the active store
        on the spent generation, and on the refusal path would discard the
        only live copy of that lineage. Detecting an expired FOREIGN
        credential is real and belongs on that machinery, not here; the field
        incident's shape is a live token, which this still catches.

        ADVISORY, in the same direction the oracle is everywhere else: a
        ``None`` answer means UNRESOLVABLE (offline, 401, schema drift), never
        "wrong", and must not block a registration that worked before this
        guard existed. Only a resolved identity that DISAGREES refuses. One
        level down, ``organizationUuid: None`` means the profile response
        carried no organization block at all — structurally ABSENT, not
        "personal". That is unverifiable ONLY about the org, exactly like the
        class's own ``_resolved_matches_slot_identity``: its
        ``if r_org is None: return None`` sits inside the branch where the
        email already matches, so it never excuses a disagreeing email --
        only a matching email gets the benefit of an absent org.
        """
        def unverified(why: str) -> str:
            # Fail-open, but never silently: registering with the ownership
            # question unanswered is the state the field incident was in.
            print(
                f"{accent('Notice:')} could not verify that the stored "
                f"credential belongs to {email} ({why}). Registering anyway; "
                f"re-run where the check can complete to confirm."
            )
            return creds

        token = oauth.extract_access_token(creds)
        if not token:
            return unverified("no access token to resolve")
        oauth_data = oauth.extract_oauth_data(creds)
        if oauth_data and oauth.is_oauth_token_expired(oauth_data.get("expiresAt")):
            # Unresolvable, exactly like an offline profile fetch. NOT a
            # refresh: consuming a grant retires its predecessor server-side,
            # and every other caller that does it holds the account FileLock
            # and CC's credential locks and persists the successor to BOTH
            # stores. A bare refresh here would leave the active store on the
            # spent generation, and on the refusal path would discard the only
            # live copy of that lineage.
            return unverified("the access token is expired")
        profile = oauth.fetch_oauth_profile(token)
        if not profile:
            return unverified("the identity lookup did not resolve")
        # Uuid first, like ``_resolved_matches_slot_identity``: uuids are
        # stable where an email can be recycled across accounts. The EMAIL is
        # consulted only without a stored uuid; the org block below runs on
        # both arms.
        seen_uuid = (profile.get("uuid") or "").strip()
        if account_uuid:
            # Falls THROUGH to the org corroboration below on a match. Returning
            # here would accept a uuid match under a disagreeing org, which
            # ``_resolved_matches_slot_identity`` calls another account
            # whenever BOTH orgs are present -- and it would make the org
            # message below unreachable for every config Claude Code writes,
            # since those all carry a uuid. This guard is stricter than the
            # sibling when one side's org is absent, deliberately: a capture
            # is a one-time write, not a per-switch check.
            if seen_uuid != account_uuid:
                raise ConfigError(
                    f"The stored credential does not belong to {email}: the "
                    f"token resolves to account {seen_uuid}, not {account_uuid}. "
                    f"Nothing was changed. This happens when the config names "
                    f"one account while the credential store still holds "
                    f"another's token (e.g. a renamed .claude.json over a live "
                    f"keychain item). Log in as {email} in THIS environment, "
                    f"then re-run."
                )
        else:
            seen = (profile.get("email") or "").strip()
            if not seen:
                return unverified("the resolved identity carries no address")
            if seen.lower() != email.lower():
                raise ConfigError(
                    f"The stored credential does not belong to {email}: the "
                    f"token resolves to {seen}. Nothing was changed. This "
                    f"happens when the config names one account while the "
                    f"credential store still holds another's token (e.g. a "
                    f"renamed .claude.json over a live keychain item). Log in "
                    f"as {email} in THIS environment, then re-run."
                )
        resolved_org = profile.get("organizationUuid")
        if resolved_org is None:
            return creds                      # structurally absent -- unverifiable
        seen_org = resolved_org.strip()
        if seen_org == (org_uuid or ""):
            return creds
        # Same address, different org: naming the address twice says
        # nothing (it's the address that agrees) -- name the two
        # organizations that disagree instead.
        raise ConfigError(
            f"The stored credential for {email} belongs to organization "
            f"{seen_org or 'personal'}, not {org_uuid or 'personal'}. "
            f"Nothing was changed. Two accounts can share an email "
            f"across organizations. Log in as {email} in the "
            f"{org_uuid or 'personal'} organization in THIS environment, "
            f"then re-run."
        )

    def _reject_live_api_key_capture(self, creds: str) -> None:
        """Guard for ``add_account``: never capture a live managed key as OAuth.

        ``add_account`` snapshots the *live* active credential under an
        ``oauthAccount`` identity. Now that ``_read_credentials`` can return a raw
        ``sk-ant-api…`` key, a live ``/login`` key could be backed up as a kindless
        account, corrupting the session-guard / export / collision logic that keys
        off ``kind``. Reject with guidance toward the supported path instead.
        """
        if looks_like_api_key(creds):
            raise ValidationError(
                "Active login is an API-key account. Add it with "
                "'openswap --add-token sk-ant-api...' instead of --add-account."
            )

    def _reject_cross_kind_collision(self, email: str, is_api_key: bool) -> None:
        """Reject registering a token whose (email, personal-org) already exists as
        the *other* kind.

        Identity is matched on ``(email, organizationUuid)`` only, so two slots
        sharing an email across kinds (one OAuth, one API key) could not be told
        apart at switch time. Rather than thread ``kind`` through the whole identity
        system, refuse the collision and point the user at a distinct ``--email``.
        The default ``…@token.local`` labels never collide; this only guards a forced
        ``--email``.
        """
        data = self._get_sequence_data()
        if not data:
            return
        slot = self._find_account_slot(data, email, "")
        if slot is None:
            return
        existing_kind = self._account_kind(slot)
        new_kind = "api_key" if is_api_key else "oauth"
        if existing_kind != new_kind:
            existing_label = "API-key" if existing_kind == "api_key" else "OAuth"
            new_label = "API-key" if is_api_key else "OAuth"
            raise ValidationError(
                f"'{email}' already exists as an {existing_label} account "
                f"(slot {slot}); cannot add it as an {new_label} account. "
                f"Pass a distinct --email."
            )

    def add_account(
        self,
        slot: int | None = None,
        assume_yes: bool = False,
        alias: str | None = None,
    ) -> None:
        """Add current account to managed accounts.

        Args:
            slot: Specify the slot number to store the account in.
                  When None, auto-assigns the next available number.
                  When specified, prompts for confirmation if the slot
                  is already occupied by a different account.
            assume_yes: Skip that overwrite prompt (callers with their own
                  confirmation UI, e.g. the extra, confirm before calling).
            alias: Optional short display alias to set on this account.
                  When omitted, an existing alias on the slot is preserved.
        """
        self._refuse_session_shell()
        if alias is not None:
            try:
                alias = normalize_alias(alias)
            except ValueError as e:
                raise ValidationError(str(e)) from e

        self._commit_add_account(slot, assume_yes, alias)

    def _read_capture_config(self, verified: tuple[str, str, str]) -> tuple[str, dict]:
        """The bytes to back up and their ``oauthAccount``, from ONE read.

        The bytes must still describe ``verified``: the drift guard re-reads
        the file later and cannot see a login that landed and settled back in
        between, so the check has to be on the bytes themselves. Unparseable
        or account-less content refuses too.
        """
        config_path = self._get_claude_config_path()
        try:
            text = read_text_with_retry(config_path)
        except FileNotFoundError:
            raise ConfigError("Claude config file not found")
        except PermissionError:
            raise ConfigError("Permission denied reading Claude config")
        except UnicodeDecodeError as e:
            raise ConfigError(
                f"{config_path} could not be decoded ({e}). Retry once Claude Code "
                "has finished writing it."
            ) from e
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ConfigError(
                f"{config_path} could not be parsed ({e}). Retry once Claude Code "
                "has finished writing it."
            ) from e
        oauth = data.get("oauthAccount") if isinstance(data, dict) else None
        if not isinstance(oauth, dict):
            oauth = {}
        found = (
            oauth.get("emailAddress", "") or "",
            oauth.get("organizationUuid", "") or "",
            oauth.get("accountUuid", "") or "",
        )
        if found != verified:
            raise ConfigError(
                f"The active Claude login changed while {verified[0]} was being "
                "captured; nothing was stored. Retry."
            )
        return text, oauth

    def _commit_add_account(
        self,
        slot: int | None,
        assume_yes: bool,
        alias: str | None,
    ) -> None:
        """Read live login, prompt if needed, then commit under ``lock_file``."""
        with FileLock(self.lock_file):
            self._setup_directories()
            self._init_sequence_file()
            self._migrate_org_fields()

        try:
            identity = self._get_current_identity_triple(strict=True)
        except ConfigError as e:
            # The strict reader's generic "repair or move it" is wrong for a
            # file Claude Code is mid-write on; a parse error here wants a
            # retry, not surgery. An OSError keeps its own accurate advice.
            if isinstance(e.__cause__, (json.JSONDecodeError, UnicodeDecodeError)):
                raise ConfigError(
                    f"Could not read {self._get_claude_config_path()}: "
                    f"{e.__cause__}. Retry once Claude Code has finished writing it."
                ) from e
            raise
        if identity is None:
            raise NotLoggedInError("No active Claude account found. Please log in first.")
        current_email, current_org_uuid, current_account_uuid = identity

        # When no slot specified and account already exists, refresh credentials in place
        if slot is None and self._account_exists(current_email, current_org_uuid):
            seq = self._get_sequence_data()
            account_num = self._find_account_slot(seq, current_email, current_org_uuid)
            matched_org_name = seq["accounts"][account_num].get("organizationName", "") if account_num else ""

            if alias is not None:
                conflict = self._alias_in_use(alias, exclude_num=account_num)
                if conflict is not None:
                    raise ValidationError(
                        f"Alias '{alias}' is already used by account {conflict}"
                    )

            current_creds = self._read_capture_credentials()
            if current_creds is None:
                raise CredentialReadError("Failed to read credentials for current account")
            if not current_creds:
                raise CredentialReadError("No credentials found for current account")
            self._reject_live_api_key_capture(current_creds)
            current_creds = self._reject_foreign_credential_capture(
                current_creds, current_email, current_org_uuid,
                current_account_uuid,
            )
            self._reject_credential_drift_since_verify(current_creds)

            current_config, _ = self._read_capture_config(identity)

            # AFTER the read, because it licenses those bytes. Ahead of it, a
            # `/login` landing between the check and the read stores a config
            # the check never saw. The create path below has always had this
            # order.
            #
            # THE TRIPLE THAT WAS READ, never a rebuild from the unpacked
            # names. A sibling change overwrites two of them with an un-spliced
            # email and org while leaving the third literal, and the mix
            # describes no real account -- so the guard would compare it against
            # a fresh read, never match, and refuse every time instead of only
            # on a race.
            self._reject_identity_drift_since_verify(identity)

            with FileLock(self.lock_file):
                seq = self._get_sequence_data() or {}
                account_num = self._find_account_slot(
                    seq, current_email, current_org_uuid
                )
                if not account_num:
                    raise AccountNotFoundError(
                        f"No account found with identifier: {current_email}"
                    )
                if alias is not None:
                    conflict = self._alias_in_use(alias, exclude_num=account_num)
                    if conflict is not None:
                        raise ValidationError(
                            f"Alias '{alias}' is already used by account {conflict}"
                        )
                matched_org_name = seq["accounts"][account_num].get(
                    "organizationName", ""
                )
                self._write_account_credentials(
                    account_num, current_email, current_creds
                )
                self._write_account_config(
                    account_num, current_email, current_config
                )
                self._usage_store.clear_dead_token(
                    [account_num], {account_num: (current_email, current_org_uuid)}
                )
                if alias is not None:
                    seq["accounts"][account_num]["alias"] = alias
                seq["activeAccountNumber"] = int(account_num)
                seq["lastUpdated"] = get_timestamp()
                self._write_json(self.sequence_file, seq)

            tag = self._get_display_tag(current_email, matched_org_name, current_org_uuid)
            self._logger.info(f"Updated credentials for account {account_num}: {current_email}")
            print(
                f"{accent('Updated credentials')} for Account {account_num} "
                f"({current_email} {muted(f'[{tag}]')})."
            )
            return

        # Determine slot number and collect confirmation decisions
        # (no destructive operations until new account is verified readable)
        displace_slot = None  # slot to clean up (occupied by different account)
        migrate_from = None   # old slot to clean up (same account, different slot)

        if slot is not None:
            if slot < 1:
                raise ConfigError("Slot number must be >= 1")
            account_num = str(slot)
            data = self._get_sequence_data()

            # Find if current account already exists in a different slot
            if self._account_exists(current_email, current_org_uuid):
                old_num = self._find_account_slot(
                    data, current_email, current_org_uuid
                )
                if old_num and old_num != account_num:
                    migrate_from = old_num

            # Check if target slot is occupied by a different account
            if account_num in data.get("accounts", {}):
                existing = data["accounts"][account_num]
                existing_email = existing.get("email", "unknown")
                is_same = (existing_email == current_email
                           and existing.get("organizationUuid", "") == current_org_uuid)
                if not is_same:
                    existing_tag = self._get_display_tag(
                        existing_email,
                        existing.get("organizationName", ""),
                        existing.get("organizationUuid", ""),
                    )
                    warning(f"Slot {slot} already occupied")
                    print(
                        f"{existing_email} {muted(f'[{existing_tag}]')}"
                    )
                    if not assume_yes:
                        try:
                            answer = input(f"Overwrite slot {slot}? [y/N] ").strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            print(f"\n{dimmed('Cancelled')}")
                            return
                        if answer not in ("y", "yes"):
                            print(dimmed("Cancelled"))
                            return
                    displace_slot = (
                        account_num,
                        existing_email,
                        existing.get("organizationUuid", "") or "",
                    )
        else:
            account_num = str(self._get_next_account_number())

        # Capture any alias to carry forward before destructive cleanup below
        # deletes the old record (same account moving slots, or refreshing in place).
        existing_alias = None
        if slot is not None:
            prior = data.get("accounts", {}).get(account_num) or {}
            if (
                prior.get("email") == current_email
                and prior.get("organizationUuid", "") == current_org_uuid
            ):
                existing_alias = prior.get("alias")
            if migrate_from:
                existing_alias = data["accounts"][migrate_from].get("alias") or existing_alias

        if alias is not None:
            conflict = self._alias_in_use(alias, exclude_num=account_num)
            if conflict is not None:
                raise ValidationError(
                    f"Alias '{alias}' is already used by account {conflict}"
                )

        # Read new account credentials BEFORE any destructive operations
        current_creds = self._read_capture_credentials()
        if current_creds is None:
            raise CredentialReadError("Failed to read credentials for current account")
        if not current_creds:
            raise CredentialReadError("No credentials found for current account")
        self._reject_live_api_key_capture(current_creds)
        current_creds = self._reject_foreign_credential_capture(
            current_creds, current_email, current_org_uuid, current_account_uuid
        )
        self._reject_credential_drift_since_verify(current_creds)

        current_config, oauth_data = self._read_capture_config(identity)
        account_uuid = current_account_uuid
        organization_uuid = current_org_uuid
        organization_name = oauth_data.get("organizationName", "") or ""

        self._reject_identity_drift_since_verify(identity)

        prune_identity = None
        with FileLock(self.lock_file):
            data = self._get_sequence_data() or {
                "activeAccountNumber": None,
                "lastUpdated": "",
                "sequence": [],
                "accounts": {},
            }
            if slot is None:
                account_num = str(self._get_next_account_number())
            else:
                account_num = str(slot)
                existing = data.get("accounts", {}).get(account_num)
                if existing:
                    is_same = (
                        existing.get("email") == current_email
                        and existing.get("organizationUuid", "") == current_org_uuid
                    )
                    if not is_same:
                        if displace_slot is None:
                            raise ConfigError(
                                f"Slot {slot} is occupied; nothing was added. Retry."
                            )
                        d_num, d_email, d_org = displace_slot
                        if (
                            existing.get("email") != d_email
                            or (existing.get("organizationUuid", "") or "") != d_org
                        ):
                            raise ConfigError(
                                f"Slot {slot} occupant changed; nothing was added. Retry."
                            )
                else:
                    displace_slot = None
            migrate_from = None
            old_num = self._find_account_slot(data, current_email, current_org_uuid)
            if old_num and old_num != account_num:
                migrate_from = old_num
            if alias is not None:
                conflict = self._alias_in_use(alias, exclude_num=account_num)
                if conflict is not None:
                    raise ValidationError(
                        f"Alias '{alias}' is already used by account {conflict}"
                    )
            existing_alias = None
            prior = data.get("accounts", {}).get(account_num) or {}
            if (
                prior.get("email") == current_email
                and prior.get("organizationUuid", "") == current_org_uuid
            ):
                existing_alias = prior.get("alias")
            if migrate_from:
                existing_alias = (
                    data["accounts"][migrate_from].get("alias") or existing_alias
                )

            if displace_slot:
                self._ensure_no_live_session(
                    displace_slot[0], displace_slot[1], "the operation"
                )
            if migrate_from:
                self._ensure_no_live_session(
                    migrate_from,
                    data["accounts"][migrate_from].get("email", ""),
                    "the operation",
                )

            self._write_account_credentials(account_num, current_email, current_creds)
            self._write_account_config(account_num, current_email, current_config)
            self._usage_store.clear_dead_token(
                [account_num], {account_num: (current_email, organization_uuid)}
            )

            stale_files: list[tuple[str, str]] = []
            if displace_slot:
                d_num, d_email, d_org = displace_slot
                stale_files.append((d_num, d_email))
                if int(d_num) in data["sequence"]:
                    data["sequence"].remove(int(d_num))
                del data["accounts"][d_num]
                prune_identity = (d_email, d_org)

            if migrate_from:
                old_email = data["accounts"][migrate_from].get("email", "")
                stale_files.append((migrate_from, old_email))
                if int(migrate_from) in data["sequence"]:
                    data["sequence"].remove(int(migrate_from))
                del data["accounts"][migrate_from]

            data["accounts"][account_num] = {
                "email": current_email,
                "uuid": account_uuid,
                "organizationUuid": organization_uuid,
                "organizationName": organization_name,
                "added": get_timestamp(),
            }
            carried_alias = alias if alias is not None else existing_alias
            if carried_alias:
                data["accounts"][account_num]["alias"] = carried_alias
            if int(account_num) not in data["sequence"]:
                data["sequence"].append(int(account_num))
                data["sequence"].sort()
            data["activeAccountNumber"] = int(account_num)
            data["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, data)
            for stale_num, stale_email in stale_files:
                if stale_num == account_num and stale_email == current_email:
                    continue
                self._delete_account_files(stale_num, stale_email)

        if prune_identity:
            self._prune_mappings(*prune_identity)
        tag = self._get_display_tag(current_email, organization_name, organization_uuid)
        self._logger.info(f"Added account {account_num}: {current_email} (org: {organization_uuid or 'personal'})")
        if migrate_from:
            print(f"{dimmed(f'Moved from slot {migrate_from} → {slot}')}")
        print(f"{accent('Added')} Account {account_num}: {current_email} {muted(f'[{tag}]')}")

    def add_account_from_token(
        self,
        token: str,
        email: str | None = None,
        slot: int | None = None,
        assume_yes: bool = False,
    ) -> None:
        """Register a raw OAuth setup-token or managed API key as a new account.

        Useful for headless servers or when the token is received from another
        machine, without needing a prior Claude Code login on this machine. The
        token type is auto-detected: an ``sk-ant-api…`` value is a managed API key
        (stored raw, activated on Claude Code's API-key auth axis), anything else is
        treated as an OAuth setup-token. No Anthropic API calls are made.

        Args:
            token: Raw OAuth setup-token or ``sk-ant-api…`` key, or ``"-"`` to read
                   one line from stdin, or ``""`` to prompt securely via getpass.
            email: Email address to associate with the account. When omitted,
                   defaults to ``setup-token-{slot}@token.local`` (or
                   ``api-key-{slot}@token.local`` for API keys) since these tokens
                   carry no real email metadata.
            slot:  Slot number to use; auto-assigned when ``None``.
            assume_yes: Skip the occupied-slot overwrite prompt (callers with
                   their own confirmation UI, e.g. the extra, confirm first).
        """
        self._refuse_session_shell()
        import getpass

        if token == "-":
            token = sys.stdin.readline().rstrip("\n")
        elif not token:
            token = getpass.getpass("Token: ")

        token = token.strip()
        if not token:
            raise ValidationError("Token cannot be empty")

        is_api_key = looks_like_api_key(token)

        if email and not self._validate_email(email):
            raise ValidationError(f"Invalid email format: {email}")

        self._commit_add_account_from_token(
            token, email, slot, assume_yes, is_api_key
        )

    def _commit_add_account_from_token(
        self,
        token: str,
        email: str | None,
        slot: int | None,
        assume_yes: bool,
        is_api_key: bool,
    ) -> None:
        """Prompt if needed, then commit the token account under ``lock_file``."""
        with FileLock(self.lock_file):
            self._setup_directories()
            self._init_sequence_file()
            self._migrate_org_fields()

        # Synthesize a placeholder email when one isn't provided. These tokens
        # have no real email metadata, so requiring users to invent one is
        # noise; the slot number gives every default account a unique key.
        if not email:
            if slot is None:
                slot = self._get_next_account_number()
            label = "api-key" if is_api_key else "setup-token"
            email = f"{label}-{slot}@token.local"

        # Don't silently overwrite/convert an existing account of the other kind:
        # identity is matched on (email, org) only, so an api-key and an OAuth
        # account sharing an email would be indistinguishable at switch time.
        self._reject_cross_kind_collision(email, is_api_key)

        # Build the credential payload by kind: a managed key is stored raw; an
        # OAuth setup-token is wrapped in Claude Code's credential JSON. The
        # synthesized config is identical for both (no real org metadata).
        if is_api_key:
            credentials = token
        else:
            credentials = json.dumps({
                "claudeAiOauth": {
                    "accessToken": token,
                    "scopes": list(SETUP_TOKEN_SCOPES),
                }
            })
        config = json.dumps({
            "oauthAccount": {
                "emailAddress": email,
                "accountUuid": "",
                "organizationUuid": None,
                "organizationName": None,
            }
        })

        # If the account already exists (same email, personal), refresh in place.
        if slot is None and self._account_exists(email, ""):
            with FileLock(self.lock_file):
                seq = self._get_sequence_data() or {}
                account_num = self._find_account_slot(seq, email, "")
                if account_num is None:
                    raise ConfigError(
                        f"Existing account metadata for {email} is inconsistent"
                    )
                self._write_account_credentials(account_num, email, credentials)
                self._write_account_config(account_num, email, config)
                # A refreshed credential invalidates any dead-token quarantine on this
                # slot (mirrors ``add_account``); otherwise the stale strike row keeps
                # the account stuck at "re-login needed" and it never fetches the new
                # token. Token accounts are always personal, so org is "".
                self._usage_store.clear_dead_token(
                    [account_num], {account_num: (email, "")}
                )
                seq["lastUpdated"] = get_timestamp()
                self._write_json(self.sequence_file, seq)
            kind_label = "API key" if is_api_key else "token"
            self._logger.info(f"Updated {kind_label} for account {account_num}: {email}")
            print(
                f"{accent(f'Updated {kind_label}')} for Account {account_num} "
                f"({email} {muted('[personal]')})."
            )
            return

        displace_slot = None
        migrate_from = None

        if slot is not None:
            if slot < 1:
                raise ConfigError("Slot number must be >= 1")
            account_num = str(slot)
            data = self._get_sequence_data()

            if self._account_exists(email, ""):
                old_num = self._find_account_slot(data, email, "")
                if old_num and old_num != account_num:
                    migrate_from = old_num

            if account_num in data.get("accounts", {}):
                existing = data["accounts"][account_num]
                existing_email = existing.get("email", "unknown")
                is_same = (
                    existing_email == email
                    and existing.get("organizationUuid", "") == ""
                )
                if not is_same:
                    existing_tag = self._get_display_tag(
                        existing_email,
                        existing.get("organizationName", ""),
                        existing.get("organizationUuid", ""),
                    )
                    warning(f"Slot {slot} already occupied")
                    print(f"{existing_email} {muted(f'[{existing_tag}]')}")
                    if not assume_yes:
                        try:
                            answer = input(f"Overwrite slot {slot}? [y/N] ").strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            print(f"\n{dimmed('Cancelled')}")
                            return
                        if answer not in ("y", "yes"):
                            print(dimmed("Cancelled"))
                            return
                    displace_slot = (
                        account_num,
                        existing_email,
                        existing.get("organizationUuid", "") or "",
                    )
        else:
            account_num = str(self._get_next_account_number())

        prune_identity = None
        with FileLock(self.lock_file):
            data = self._get_sequence_data() or {
                "activeAccountNumber": None,
                "lastUpdated": "",
                "sequence": [],
                "accounts": {},
            }
            if slot is None:
                account_num = str(self._get_next_account_number())
            else:
                account_num = str(slot)
                existing = data.get("accounts", {}).get(account_num)
                if existing:
                    is_same = (
                        existing.get("email") == email
                        and existing.get("organizationUuid", "") == ""
                    )
                    if not is_same:
                        if displace_slot is None:
                            raise ConfigError(
                                f"Slot {slot} is occupied; nothing was added. Retry."
                            )
                        d_num, d_email, d_org = displace_slot
                        if (
                            existing.get("email") != d_email
                            or (existing.get("organizationUuid", "") or "") != d_org
                        ):
                            raise ConfigError(
                                f"Slot {slot} occupant changed; nothing was added. Retry."
                            )
                else:
                    displace_slot = None
            migrate_from = None
            old_num = self._find_account_slot(data, email, "")
            if old_num and old_num != account_num:
                migrate_from = old_num

            if displace_slot:
                self._ensure_no_live_session(
                    displace_slot[0], displace_slot[1], "the operation"
                )
            if migrate_from:
                self._ensure_no_live_session(
                    migrate_from,
                    data["accounts"][migrate_from].get("email", ""),
                    "the operation",
                )

            self._write_account_credentials(account_num, email, credentials)
            self._write_account_config(account_num, email, config)
            self._usage_store.clear_dead_token(
                [account_num], {account_num: (email, "")}
            )

            stale_files: list[tuple[str, str]] = []
            if displace_slot:
                d_num, d_email, d_org = displace_slot
                stale_files.append((d_num, d_email))
                if int(d_num) in data["sequence"]:
                    data["sequence"].remove(int(d_num))
                del data["accounts"][d_num]
                prune_identity = (d_email, d_org)

            if migrate_from:
                old_email = data["accounts"][migrate_from].get("email", "")
                stale_files.append((migrate_from, old_email))
                if int(migrate_from) in data["sequence"]:
                    data["sequence"].remove(int(migrate_from))
                del data["accounts"][migrate_from]

            record = {
                "email": email,
                "uuid": "",
                "organizationUuid": "",
                "organizationName": "",
                "added": get_timestamp(),
            }
            if is_api_key:
                record["kind"] = "api_key"
            data["accounts"][account_num] = record
            if int(account_num) not in data["sequence"]:
                data["sequence"].append(int(account_num))
                data["sequence"].sort()
            data["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, data)
            for stale_num, stale_email in stale_files:
                if stale_num == account_num and stale_email == email:
                    continue
                self._delete_account_files(stale_num, stale_email)

        if prune_identity:
            self._prune_mappings(*prune_identity)
        source_label = "API key" if is_api_key else "token"
        self._logger.info(f"Added account {account_num} from {source_label}: {email}")
        if migrate_from:
            print(f"{dimmed(f'Moved from slot {migrate_from} → {slot}')}")
        print(
            f"{accent('Added')} Account {account_num}: {email} "
            f"{muted('[personal]')} {muted(f'(from {source_label})')}"
        )

    def _select_best_switchable(
        self,
        current_num: str | None,
        models: tuple[str, ...] = (),
        usage: dict | None = None,
    ) -> tuple[str | None, str]:
        """Decide the ``best`` strategy target relative to the current account.

        Compares the rate-limit headroom of every *other* switchable account
        against the current one and only recommends a switch it can *prove*
        lands on strictly more headroom — never onto an account worse than (or
        merely unverifiable against) where the user already is. When a switch
        can't be proven beneficial, it stays put; bare ``openswap --switch``
        remains the way to force a plain rotation. ``models`` folds the named
        per-model weekly windows into every headroom comparison (see
        ``oauth.account_headroom``). Returns ``(target, note)``:

        - ``(num, "")`` — switch to ``num`` (strictly more headroom than current)
        - ``(None, "current-unavailable")`` — current account's usage is unknown,
          so no comparison is possible → stay
        - ``(None, "no-comparison")`` — no other account has known usage → stay
        - ``(None, "incomplete-comparison")`` — current is best among the
          accounts we can measure, but some candidate's usage is unknown, so we
          can't claim it's the best or that everything is exhausted → stay
        - ``(None, "stay")`` — current account provably has the most headroom
        - ``(None, "exhausted")`` — current is the best and every account is at
          its limit (switching would not help) → stay
        - ``(None, "none")`` — no other switchable account exists

        Ties (including current-vs-other) resolve in favour of staying put.
        Never raises on network failure.
        """
        data = self._get_sequence_data() or {}
        others = [
            str(n) for n in data.get("sequence", [])
            if str(n) != str(current_num)
            and self._account_is_switchable(str(n))
            and not self._disabled_from_data(data, str(n))
        ]
        if not others:
            return None, "none"

        if usage is None:
            usage = self._usage_by_account()
        current_headroom = oauth.account_headroom(usage.get(str(current_num)), models)
        if current_headroom is None:
            # Can't measure where the user is → can't prove any target is
            # better. Stay rather than risk moving onto a worse account.
            return None, "current-unavailable"

        scored = [
            (oauth.account_headroom(usage.get(num), models), num) for num in others
        ]
        known = [(h, num) for h, num in scored if h is not None]
        if not known:
            return None, "no-comparison"

        # max() keeps the first maximal element; `known` preserves rotation
        # order, so ties resolve to the earliest slot.
        best_headroom, best_num = max(known, key=lambda t: t[0])
        if best_headroom > current_headroom:
            return best_num, ""

        # Current is at least as good as every account we can measure. Stay —
        # but only claim "all exhausted" when every candidate's usage is known.
        if any(h is None for h, _ in scored):
            return None, "incomplete-comparison"
        if current_headroom <= 0:
            return None, "exhausted"
        return None, "stay"

    def _first_run_setup(self) -> None:
        """First-run setup workflow."""
        identity = self._get_current_account()

        if identity is None:
            print(dimmed("No active Claude account found. Please log in first."))
            return
        current_email, _ = identity

        response = input(
            f"No managed accounts found. Add current account "
            f"({current_email}) to managed list? [Y/n] "
        )
        if response.lower() == "n":
            print(dimmed("Setup cancelled. You can run 'openswap --add-account' later."))
            return

        self.add_account()

    def _switch_result_from_op(
        self, op: dict, strategy: str, extra_warnings: list[str] | None = None
    ) -> dict:
        """Build a switch result from a ``_perform_switch`` return value.

        ``switched`` is derived from whether the live identity actually changed
        (``from != to``) — covering recorded/live drift in plain rotation, not just
        ``switch_to`` onto the already-active account.
        """
        from_ref = op["from"]
        to_ref = op["to"]
        switched = from_ref != to_ref
        if switched:
            reason = "switched"
            message = f"Switched to Account-{to_ref['number']} ({to_ref['email']})"
        else:
            reason = "already-active"
            message = f"Already on Account-{to_ref['number']} ({to_ref['email']})"
        return {
            "schemaVersion": SCHEMA_VERSION,
            "switched": switched,
            "from": from_ref,
            "to": to_ref,
            "strategy": strategy,
            "reason": reason,
            "message": message,
            "warnings": (extra_warnings or []) + op["warnings"],
        }

    def _switch_noop(
        self,
        *,
        strategy: str,
        reason: str,
        message: str,
        from_ref: dict | None = None,
        to_ref: dict | None = None,
        warnings: list[str] | None = None,
    ) -> dict:
        """Build a no-op switch result (``switched: false``).

        For a no-op the user neither left nor arrived anywhere — ``from`` and
        ``to`` are both the current account. Callers pass ``to_ref`` (where they
        stayed); ``from_ref`` defaults to it so every ``switched: false`` payload
        reports ``from == to``.
        """
        if from_ref is None:
            from_ref = to_ref
        return {
            "schemaVersion": SCHEMA_VERSION,
            "switched": False,
            "from": from_ref,
            "to": to_ref,
            "strategy": strategy,
            "reason": reason,
            "message": message,
            "warnings": warnings or [],
        }

    def switch(
        self,
        strategy: str | None = None,
        json_output: bool = False,
        models: tuple[str, ...] = (),
        model_source: str | None = None,
    ) -> dict | None:
        """Switch to next account in sequence.

        Args:
            strategy: Usage-aware target selection. ``"best"`` jumps to the
                  switchable account with the most remaining 5h/7d quota instead
                  of advancing the rotation; ``"next-available"`` rotates to the
                  next account, skipping any currently at its 5h/7d limit. ``None``
                  (the default) performs a plain rotation.
            models: Per-model weekly windows folded into every usage
                  comparison of the usage-aware strategies (parsed display
                  names, or the ``all`` sentinel — see
                  ``oauth.relevant_windows``). Empty = 5h/7d only.
            model_source: Where ``models`` came from (``"cli"`` or
                  ``"autoswitch.model"``) — announced up front so a config
                  fallback silently steering the pick is impossible.

        ``"best"`` only switches when it can prove another account has more
        remaining quota; if usage can't be fetched or no candidate is provably
        better, it stays put (run a plain ``openswap --switch`` to rotate anyway).
        ``"next-available"`` rotates and skips accounts at their limit, falling
        back to plain rotation when usage is unavailable. Both apply only to the
        normal path (a live Claude login present); the fresh-machine path (no
        live login, e.g. right after --import) ignores them.
        """
        strategy_label = strategy if strategy in ("best", "next-available") else "rotation"
        warnings: list[str] = []
        if strategy_label == "rotation":
            models = ()  # model limits only steer the usage-aware strategies
        if models and not json_output:
            source = "--model" if model_source == "cli" else model_source
            print(dimmed(
                f"Using configured model limits: {', '.join(models)}"
                + (f" (from {source})" if source else "")
            ))

        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        identity = self._get_current_account()

        # Ensure org fields are migrated before checking composite key
        self._get_sequence_data_migrated()

        # Fresh-machine path: no live Claude session, but we have managed accounts
        # (e.g. right after openswap --import). Activate the recorded
        # activeAccountNumber, or fall back to the first slot in sequence.
        # With no live state to capture, the target must have valid backups —
        # walk the sequence if the preferred target is broken.
        if identity is None:
            data = self._get_sequence_data() or {}
            sequence = data.get("sequence", [])
            preferred = data.get("activeAccountNumber")
            if not preferred and sequence:
                preferred = sequence[0]
            if not preferred:
                raise ConfigError("No accounts are managed yet")

            target = str(preferred)
            target_disabled = self._disabled_from_data(data, target)
            if target_disabled or not self._account_is_switchable(target):
                if target_disabled:
                    reason = console_reason = "(disabled)"
                else:
                    reason = "(no stored credentials/config)"
                    console_reason = (
                        "(no stored credentials/config, re-add with "
                        f"openswap --add-account --slot {target})"
                    )
                if json_output:
                    warnings.append(f"Skipped Account-{target} {reason}")
                else:
                    print(f"{accent('Skipping')} Account-{target} {console_reason}")
                fallback = next(
                    (str(num) for num in sequence
                     if str(num) != target
                     and not self._disabled_from_data(data, str(num))
                     and self._account_is_switchable(str(num))),
                    None,
                )
                if not fallback:
                    if any(
                        self._account_is_switchable(str(num)) for num in sequence
                    ):
                        raise ConfigError(
                            "No accounts remain in rotation. Re-enable one with: "
                            "openswap enable <num|email>"
                        )
                    raise ConfigError(
                        "No managed accounts have valid stored credentials/config. "
                        "Re-add a slot with: openswap --add-account --slot <number>"
                    )
                target = fallback
            op = self._perform_switch(target, emit_output=not json_output)
            return (
                self._switch_result_from_op(op, strategy_label, warnings)
                if json_output else None
            )

        current_email, current_org_uuid = identity

        # Check if current account is managed
        if not self._account_exists(current_email, current_org_uuid):
            # In JSON mode, don't silently auto-add (a surprising side effect in
            # automation) — report it as a structured no-op instead.
            if json_output:
                ref = account_ref(None, current_email)
                return self._switch_noop(
                    strategy=strategy_label,
                    reason="unmanaged-account",
                    from_ref=ref,
                    to_ref=ref,
                    message="Active account is not managed; run openswap --add-account",
                )
            print(f"{accent('Notice:')} Active account '{current_email}' was not managed.")
            self.add_account()
            data = self._get_sequence_data()
            account_num = data.get("activeAccountNumber")
            print(f"It has been automatically added as Account-{account_num}.")
            print(dimmed("Please run the switch command again to switch to the next account."))
            return None

        data = self._get_sequence_data()
        sequence = data.get("sequence", [])

        if len(sequence) < 2:
            if json_output:
                num = self._find_account_slot(data, current_email, current_org_uuid)
                return self._switch_noop(
                    strategy=strategy_label,
                    reason="only-one-account",
                    to_ref=account_ref(int(num), current_email) if num else None,
                    message="Only one account is managed. Add more accounts to switch between.",
                )
            print(dimmed("Only one account is managed. Add more accounts to switch between."))
            return None

        active_account = data.get("activeAccountNumber")
        # Where the user actually is right now (live identity), falling back to
        # the recorded active slot. Used so usage-aware switching never moves
        # them onto an account worse than their current one.
        current_num = self._find_account_slot(data, current_email, current_org_uuid)
        if current_num is None:
            current_num = str(active_account) if active_account is not None else None

        current_ref = (
            account_ref(int(current_num), current_email) if current_num else None
        )

        # Usage-aware "jump to most headroom". Only switches when another
        # account is provably better; otherwise stays put (never moves onto a
        # worse or unverifiable account). Bare `openswap --switch` rotates anyway.
        if strategy == "best":
            best_usage = self._usage_by_account()
            self._warn_inert_models(best_usage, models, json_output, warnings)
            target, note = self._select_best_switchable(
                current_num, models, best_usage
            )
            if target is not None:
                op = self._perform_switch(target, emit_output=not json_output)
                return (
                    self._switch_result_from_op(op, strategy_label, warnings)
                    if json_output else None
                )
            if note == "current-unavailable":
                if json_output:
                    return self._switch_noop(
                        strategy=strategy_label, reason="usage-unavailable",
                        to_ref=current_ref, warnings=warnings,
                        message=(
                            f"Current account usage is unavailable — staying on "
                            f"Account-{current_num}."
                        ),
                    )
                print(dimmed(
                    f"Current account usage is unavailable — staying on "
                    f"Account-{current_num}. Run openswap --switch to rotate."
                ))
                return None
            if note == "no-comparison":
                if json_output:
                    return self._switch_noop(
                        strategy=strategy_label, reason="usage-unavailable",
                        to_ref=current_ref, warnings=warnings,
                        message=(
                            f"No other account has usage data to compare — staying "
                            f"on Account-{current_num}."
                        ),
                    )
                print(dimmed(
                    f"No other account has usage data to compare — staying on "
                    f"Account-{current_num}. Run openswap --switch to rotate."
                ))
                return None
            if note == "incomplete-comparison":
                if json_output:
                    return self._switch_noop(
                        strategy=strategy_label, reason="usage-unavailable",
                        to_ref=current_ref, warnings=warnings,
                        message=(
                            f"No account with known usage has more remaining quota; "
                            f"some usage is unavailable — staying on Account-{current_num}."
                        ),
                    )
                print(dimmed(
                    f"No account with known usage has more remaining quota; some "
                    f"usage is unavailable — staying on Account-{current_num}."
                ))
                return None
            if note == "stay":
                if json_output:
                    return self._switch_noop(
                        strategy=strategy_label, reason="already-best",
                        to_ref=current_ref, warnings=warnings,
                        message=(
                            f"Already on the account with the most remaining quota "
                            f"(Account-{current_num})."
                        ),
                    )
                print(
                    f"{accent('Already on the account with the most remaining quota')} "
                    f"(Account-{current_num})."
                )
                return None
            if note == "exhausted":
                # With model limits in play the binding window may be scoped.
                limits_label = "usage limits" if models else "5h/7d limit"
                if json_output:
                    return self._switch_noop(
                        strategy=strategy_label, reason="candidates-exhausted",
                        to_ref=current_ref, warnings=warnings,
                        message=(
                            f"All accounts are at their {limits_label} — staying on "
                            f"Account-{current_num}."
                        ),
                    )
                warning(
                    f"All accounts are at their {limits_label} — staying on "
                    f"Account-{current_num}."
                )
                return None
            # note == "none": fall through; rotation reports the lack of targets.

        # Find current index and get next, skipping broken candidates.
        # The active slot is never checked here — _perform_switch captures
        # live state into a fresh backup before swapping, so the active
        # slot's stored backup may be stale or absent without blocking us.
        #
        # Usage-aware rotation anchors on the live account (current_num) so it
        # never lands a no-op on the slot you're already on when the live login
        # has drifted from the recorded activeAccountNumber. Plain rotation keeps
        # anchoring on active_account for byte-for-byte unchanged behavior.
        anchor = current_num if strategy == "next-available" else active_account
        try:
            current_index = sequence.index(int(anchor))
        except (TypeError, ValueError):
            try:
                current_index = sequence.index(active_account)
            except (TypeError, ValueError):
                current_index = 0

        # Only fetch usage when needed; an empty map means the headroom check
        # below is always None (skipped), preserving the non-usage-aware path.
        usage = self._usage_by_account() if strategy == "next-available" else {}
        if strategy == "next-available":
            self._warn_inert_models(usage, models, json_output, warnings)

        next_account: str | None = None
        skipped_exhausted: list[str] = []
        for offset in range(1, len(sequence)):
            candidate = str(sequence[(current_index + offset) % len(sequence)])
            if self._disabled_from_data(data, candidate):
                if json_output:
                    warnings.append(f"Skipped Account-{candidate} (disabled)")
                else:
                    print(f"{accent('Skipping')} Account-{candidate} (disabled)")
                continue
            if not self._account_is_switchable(candidate):
                if json_output:
                    warnings.append(
                        f"Skipped Account-{candidate} (no stored credentials/config)"
                    )
                else:
                    print(
                        f"{accent('Skipping')} Account-{candidate} "
                        f"(no stored credentials/config, re-add with "
                        f"openswap --add-account --slot {candidate})"
                    )
                continue
            if strategy == "next-available":
                headroom = oauth.account_headroom(usage.get(candidate), models)
                if headroom is not None and headroom <= 0:
                    skipped_exhausted.append(candidate)
                    label = "5h/7d"
                    if models:
                        # Name what actually binds ("Fable", "5h/Fable", ...)
                        # so a config-driven skip is never mysterious.
                        at = [
                            name
                            for name, pct, _ in oauth.relevant_windows(
                                usage.get(candidate), models
                            )
                            if pct >= 100.0
                        ]
                        if at:
                            label = "/".join(at)
                    if json_output:
                        warnings.append(
                            f"Skipped Account-{candidate} (at {label} limit)"
                        )
                    else:
                        print(f"{accent('Skipping')} Account-{candidate} (at {label} limit)")
                    continue
            next_account = candidate
            break

        # Every rotation target is at its limit. Switching onto an exhausted
        # account would not help, so stay on the current one instead.
        if next_account is None and skipped_exhausted:
            # With model limits in play the binding window may be a scoped
            # one (the per-skip lines name it), so don't claim "5h/7d".
            limits_label = "usage limits" if models else "5h/7d limit"
            if json_output:
                return self._switch_noop(
                    strategy=strategy_label, reason="candidates-exhausted",
                    to_ref=current_ref, warnings=warnings,
                    message=(
                        f"All other accounts are at their {limits_label} — staying on "
                        f"Account-{current_num}."
                    ),
                )
            warning(
                f"All other accounts are at their {limits_label} — staying on "
                f"Account-{current_num}."
            )
            return None

        if next_account is None:
            if json_output:
                return self._switch_noop(
                    strategy=strategy_label, reason="no-valid-target",
                    to_ref=current_ref, warnings=warnings,
                    message="No other accounts have valid stored credentials/config.",
                )
            print(dimmed(
                "No other accounts have valid stored credentials/config.\n"
                "Re-add a skipped slot with: openswap --add-account --slot <number>"
            ))
            return None

        # Rotation anchored on a drifted activeAccountNumber can land on the
        # slot the user is already on — a self-switch would pointlessly rewrite
        # the live credentials (issue #79's hazard, on the strategy path).
        # Provenance-aware: only a no-op when the live credential matches the
        # slot's backup (or the divergence can't be classified — pre-fix
        # behavior, silent); a resolved divergence falls through so
        # _perform_switch can reconcile it.
        provenance: dict | None = None
        if next_account == current_num:
            action, provenance = self._self_switch_action(
                next_account, current_email
            )
            if action != "reconcile":
                if json_output:
                    return self._switch_noop(
                        strategy=strategy_label,
                        reason="already-active",
                        from_ref=current_ref,
                        to_ref=current_ref,
                        warnings=warnings,
                        message=f"Already on Account-{next_account} ({current_email})",
                    )
                print(
                    f"{accent('Already on')} Account-{next_account} ({current_email})"
                )
                return None

        op = self._perform_switch(
            next_account, emit_output=not json_output, provenance=provenance
        )
        return (
            self._switch_result_from_op(op, strategy_label, warnings)
            if json_output else None
        )

    def switch_to(
        self,
        identifier: str,
        json_output: bool = False,
        force: bool = False,
        force_if_live_missing: bool = False,
    ) -> dict | None:
        """Switch to specific account.

        ``force`` activates the target's stored credentials directly, skipping
        both the already-active no-op guard and the backup-current step —
        the recovery path for a live login gone stale (e.g. after --import).
        ``force_if_live_missing`` narrows that recovery for stale UI actions:
        the force is cancelled under the credential locks if usable live
        credentials appeared after the card snapshot was taken.
        """
        if force_if_live_missing and not force:
            raise ValidationError("force_if_live_missing requires force=True")
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        # Ensure org fields are migrated before resolving accounts
        self._get_sequence_data_migrated()

        # Resolve identifier
        if not identifier.isdigit():
            is_alias = self._find_account_by_alias(identifier) is not None
            if not is_alias and not self._validate_email(identifier):
                raise ValidationError(f"Invalid account identifier: {identifier}")

            # For email identifiers, handle ambiguous matches interactively —
            # except in JSON mode, where we never prompt. There we fall through
            # to _resolve_account_identifier, which raises a ConfigError listing
            # the matching slots (+ org labels) → structured error envelope.
            # Aliases are unique by construction, so they never hit this.
            if not json_output and not is_alias:
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
                    choice = input("Enter account number to switch to: ").strip()
                    if not choice.isdigit() or choice not in matches:
                        print(dimmed("Cancelled"))
                        return None
                    identifier = choice

        target_account = self._resolve_account_identifier(identifier)
        if not target_account:
            raise AccountNotFoundError(
                f"No account found with identifier: {identifier}"
            )

        data = self._get_sequence_data()
        if target_account not in data.get("accounts", {}):
            raise AccountNotFoundError(f"Account-{target_account} does not exist")

        # Short-circuit a no-op before mutating (issue #79). A self-switch
        # would first back up the live credentials into the target slot —
        # destroying a freshly imported backup with a possibly stale login —
        # then read them straight back. It also re-writes credentials, takes
        # the lock, and (on macOS) touches the Keychain for nothing. --force
        # skips this guard on purpose: its job is to rewrite the live login
        # from the stored backup. Provenance-aware (issue #117): the no-op is
        # only taken when the live credential matches the slot's backup or
        # the divergence can't be classified — pre-fix behavior, silent — and
        # a *resolved* divergence falls through so _perform_switch can
        # reconcile it.
        provenance: dict | None = None
        if not force and data:
            identity = self._get_current_account()
            if identity is not None:
                cur_slot = self._find_account_slot(data, identity[0], identity[1])
                if cur_slot == target_account:
                    action, provenance = self._self_switch_action(
                        target_account, identity[0]
                    )
                if cur_slot == target_account and action != "reconcile":
                    email = (
                        data.get("accounts", {}).get(target_account, {}).get("email", "")
                    )
                    ref = account_ref(int(target_account), email)
                    if not json_output:
                        print(
                            f"{accent('Already on')} Account-{target_account} ({email})"
                        )
                        print(dimmed(
                            "To rewrite the live login from the stored backup "
                            "(e.g. after --import), run: "
                            f"openswap --switch-to {target_account} --force"
                        ))
                        return None
                    return self._switch_noop(
                        strategy="direct",
                        reason="already-active",
                        from_ref=ref,
                        to_ref=ref,
                        message=f"Already on Account-{target_account} ({email})",
                    )

        perform_kwargs = {
            "emit_output": not json_output,
            "force_activate": force,
            "provenance": provenance,
        }
        if force_if_live_missing:
            perform_kwargs["force_if_live_missing"] = True
        op = self._perform_switch(target_account, **perform_kwargs)
        if json_output and op.get("restoreSkipped"):
            result = self._switch_noop(
                strategy="direct",
                reason="live-credential-present",
                from_ref=op["from"],
                to_ref=op["to"],
                message=(
                    "Live credentials appeared before restoration; "
                    "the saved backup was not activated"
                ),
                warnings=op["warnings"],
            )
        else:
            result = self._switch_result_from_op(op, "direct") if json_output else None
        # A forced self-activation really rewrote the live credentials from the
        # stored backup — "already-active" would misdescribe that mutation.
        # A cross-slot force stays "switched": reason reports the outcome, not
        # the skipped-backup mechanism.
        if (
            result is not None
            and force
            and not op.get("restoreSkipped")
            and not result["switched"]
        ):
            to = result["to"]
            result["reason"] = "activated"
            result["message"] = (
                f"Activated Account-{to['number']} ({to['email']}) from stored backup"
            )
        return result

    def _self_switch_action(self, slot: str, email: str) -> tuple[str, dict | None]:
        """How to treat a switch that targets the already-active slot.

        Returns ``(action, provenance)``:

        - ``("noop", None)`` — live matches the slot's backup; nothing to do
          (issue #79's short-circuit).
        - ``("reconcile", provenance)`` — live diverged and its owner was
          resolved: run the full switch so ``_perform_switch`` can classify
          (re-sync a legitimate rotation, or preserve foreign bytes and
          restore the slot's stored credential).
        - ``("noop-diverged", None)`` — live diverged but cannot be
          classified (offline / endpoint failure / no profile access). Exact
          pre-fix behavior: an ordinary already-active no-op, silent to the
          user — endpoint trouble must never surface on the self-switch path
          either. Leaving everything untouched is also the safe write:
          activating the stored backup over an unverified live credential
          could replace a freshly rotated token with its consumed ancestor.
        """
        if self._live_matches_slot_backup(slot, email):
            return "noop", None
        provenance = self._prefetch_live_identity()
        if provenance.get("resolved") is None:
            self._logger.info(
                "Live credential diverges from Account-%s's stored backup "
                "and ownership could not be verified; self-switch left "
                "everything untouched (pre-fix no-op).",
                slot,
            )
            return "noop-diverged", None
        return "reconcile", provenance

    def _classify_outgoing_credential(
        self,
        current_account: str,
        current_email: str,
        original_creds: str,
        provenance: dict,
        data: dict,
    ) -> tuple[str, str | None]:
        """Decide what the switch-time backup may do with the live credential.

        Returns ``(kind, foreign_slot)``:

        - ``"own-bytes"``      — byte-identical to the slot's stored backup;
          nothing changed, nothing to capture.
        - ``"own-family"``     — same refresh-token lineage (access token
          rotated); back up normally.
        - ``"own-rotated"``    — full rotation, but the profile endpoint
          resolved the live token to this slot's identity; back up normally
          (the live→backup re-sync that keeps slots alive across Claude
          Code's routine refresh-token rotations).
        - ``"foreign"``        — uuid-positively resolved to *another* managed
          slot (``foreign_slot``) holding a different lineage; backing it up
          here would destroy this slot's only refresh token (issue #117's
          poisoning). Preserved in a safety copy, never written into any
          slot: identity proves ownership, not generation freshness.
        - ``"foreign-synced"`` — resolved to another managed slot whose
          stored backup already holds this exact lineage; nothing needs
          preserving, nothing may be written.
        - ``"wiped"``          — an OAuth blob whose token fields are all
          empty: Claude Code's ``invalid_grant`` reaction empties
          ``accessToken``/``refreshToken`` in place, keeping the wrapper and
          metadata (observed live on 2.1.181). No token → the identity
          oracle is structurally silent, so this used to fall to
          ``"unresolved"`` and the fail-open backup copied the empty tokens
          over the slot's only surviving refresh token. Never written into
          any slot; nothing worth preserving either.
        - ``"alien"``          — a *structurally complete* identity (uuid +
          email + organization) that matches no managed slot (unmanaged
          login, recycled email wearing a managed address, or an email+org
          match without uuid confirmation). Preserved in a safety copy.
        - ``"known-foreign"``  — the switch-time oracle failed, but a
          collect-pass probe in this process already condemned this exact
          lineage (a cached definitive verdict, revalidated against the
          slot's current identity). Routed like ``"alien"``: preserved,
          never written — a transient probe failure must not let the
          fail-open backup poison the slot with bytes we have already
          proven foreign (this switch may BE the repair that verdict
          triggered).
        - ``"unresolved"``     — mismatch and identity could not be
          established (offline, endpoint failure, malformed response, no
          access token in the blob, bytes moved since the pre-lock read) —
          or was only *partially* established: a response missing email or
          organization matching nothing is indistinguishable from schema
          drift, and preserve-and-skip on drift would silently recreate the
          fail-closed behavior this design forbids. The caller falls back to
          the exact pre-fix backup: the identity oracle is advisory, and
          endpoint state must never change switch behavior beyond skipping
          the extra safety.
        """
        backup = self._read_account_credentials(current_account, current_email)
        if backup and backup == original_creds:
            return ("own-bytes", None)
        if backup and (
            oauth.credential_fingerprint(backup)
            == oauth.credential_fingerprint(original_creds)
        ):
            return ("own-family", None)
        live_oauth = oauth.extract_oauth_data(original_creds)
        if live_oauth is not None and not (
            live_oauth.get("accessToken") or live_oauth.get("refreshToken")
        ):
            return ("wiped", None)
        resolved = provenance.get("resolved")
        if resolved is None or provenance.get("live") != original_creds:
            if self._probe_verdicts.get(
                self._lineage_key(
                    current_account, current_email,
                    oauth.credential_fingerprint(original_creds) or "",
                )
            ) is False:
                return ("known-foreign", None)
            return ("unresolved", None)
        r_email = resolved.get("email") or ""
        r_org = resolved.get("organizationUuid") or ""
        r_uuid = (resolved.get("uuid") or "").strip()
        # Outgoing-slot uuid match first: robust to partial responses (a
        # drifted schema may drop email/organization) and to an account
        # whose email changed. Organization must agree only when both sides
        # record one — the codebase's usual leniency for org matching.
        own = data.get("accounts", {}).get(current_account, {})
        own_uuid = (own.get("uuid") or "").strip()
        own_org = own.get("organizationUuid", "") or ""
        if r_uuid and own_uuid and r_uuid == own_uuid and (
            not r_org or not own_org or r_org == own_org
        ):
            return ("own-rotated", None)
        slot = self._find_account_slot(data, r_email, r_org) if r_email else None
        if slot is not None and r_uuid:
            # When both sides carry a uuid it must agree: an email+org match
            # with a conflicting uuid is a *different* account wearing a
            # recycled email (e.g. deleted/recreated claude.ai account), and
            # treating it as the slot would poison the slot's backup.
            stored_uuid = (
                data.get("accounts", {}).get(slot, {}).get("uuid") or ""
            ).strip()
            if stored_uuid and stored_uuid != r_uuid:
                slot = None
        if slot is None and r_uuid:
            # Fall back to the account uuid (org-scoped) in case the slot's
            # stored email is stale or synthesized (add-token placeholder).
            for num, acct in data.get("accounts", {}).items():
                if (
                    acct.get("uuid")
                    and acct.get("uuid") == r_uuid
                    and (acct.get("organizationUuid", "") or "") == r_org
                ):
                    slot = num
                    break
        if slot == current_account:
            return ("own-rotated", None)
        if slot is None:
            # A positive "alien" needs a structurally complete identity —
            # email plus organization — matching nothing. A partial one is
            # indistinguishable from schema drift and must fail open like
            # any other oracle degradation, not preserve-and-skip.
            if r_email and resolved.get("organizationUuid") is not None:
                return ("alien", None)
            return ("unresolved", None)
        # A cross-slot attribution must be uuid-positive: an email+org match
        # against a slot with no recorded uuid (add-token placeholder) is not
        # evidence enough to name that slot in user output — treat as alien.
        stored_uuid = (
            data.get("accounts", {}).get(slot, {}).get("uuid") or ""
        ).strip()
        if not r_uuid or stored_uuid != r_uuid:
            return ("alien", None)
        foreign_email = data.get("accounts", {}).get(slot, {}).get("email", "")
        foreign_backup = self._read_account_credentials(slot, foreign_email)
        if foreign_backup and (
            foreign_backup == original_creds
            or oauth.credential_fingerprint(foreign_backup)
            == oauth.credential_fingerprint(original_creds)
        ):
            return ("foreign-synced", slot)
        return ("foreign", slot)

    def _stash_live_credential(
        self,
        original_creds: str,
        reason: str,
        current_account: str,
        resolved: dict | None,
    ) -> str:
        """Preserve an unowned live credential before it is overwritten.

        Raises on failure — a successful stash is the license to overwrite the
        live store (the bytes may be the only live copy of some account's
        refresh token). The logged evidence doubles as the instrumentation for
        identifying what wrote the credential (#117's writer is unidentified).
        """
        creds_mtime: str | None = None
        try:
            mtime = get_credentials_path().stat().st_mtime
            from datetime import datetime, timezone

            creds_mtime = datetime.fromtimestamp(
                mtime, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except OSError:
            pass  # Keychain backend or file absent
        live_oauth_account: dict | None = None
        try:
            config = self._read_json(self._get_claude_config_path())
            if isinstance(config, dict):
                live_oauth_account = config.get("oauthAccount")
        except Exception:
            pass
        entry_id = self._store._write_unclaimed_credential(
            original_creds,
            {
                "reason": reason,
                "configSlot": current_account,
                "fingerprint": oauth.credential_fingerprint(original_creds),
                "liveOauthAccount": live_oauth_account,
                "resolvedIdentity": resolved,
                "credentialsMtime": creds_mtime,
            },
        )
        self._logger.warning(
            "Live credential does not belong to Account-%s (%s): stashed as %s "
            "(credentials mtime %s). Something outside openswap rewrote the live "
            "login after the last switch.",
            current_account,
            reason,
            entry_id,
            creds_mtime or "unknown",
        )
        return entry_id

    def _read_target_credentials(self, account_num: str, email: str) -> str:
        """The switch target's stored credential, or a SwitchError naming why.

        One helper because `_perform_switch` reads the target twice — the
        direct-activation branch (fresh machine, post-import, --force) and
        the normal branch (every ordinary switch on a working install) — and
        only the first carried the unreadable check. The normal branch sent
        every ordinary `openswap switch` to "Re-add with: openswap --add-account",
        which burns the stored grant of a slot whose backup is merely behind
        a locked Keychain. `session.py`'s `_bootstrap` carried a third copy.
        """
        creds, unreadable = self._read_account_credentials_ex(
            account_num, email
        )
        if creds:
            return creds
        if unreadable:
            # The backup may exist but the Keychain cannot be read right now
            # (locked / non-GUI session) — a re-add would needlessly burn the
            # stored grant.
            raise SwitchError(
                f"Account-{account_num}'s backup is in the macOS Keychain "
                f"but it is unreadable right now (locked or no GUI "
                f"session). Retry from a GUI terminal; do not re-add."
            )
        raise SwitchError(
            f"Account-{account_num} has no stored credentials. "
            f"Re-add with: openswap --add-account --slot {account_num}"
        )

    def _refuse_session_shell(self) -> None:
        """Refuse live-store mutation from inside a leftover session-profile shell.

        A ``CLAUDE_CONFIG_DIR`` pointing inside a session profile means this
        shell's "live store" is the profile, not the default login; a
        switch/add here would splice the default sequence against the wrong
        live store (mirrors SessionManager's own guard). Called by every
        entry point that mutates the live store or the roster. There is no
        single chokepoint to hang this on: the one it used to claim was
        `_perform_switch`, which covers the switch family only, so
        `remove_account`, `swap_accounts`, `move_account`, `purge` and the
        alias setters all ran happily inside a session shell —
        `remove_account` deleting the session profile of the very shell it
        was running in. Nine call sites is the honest cost of that.
        """
        cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        if not cfg_dir:
            return
        try:
            Path(cfg_dir).resolve().relative_to(
                (self.backup_dir / "sessions").resolve()
            )
        except ValueError:
            return
        raise SwitchError(
            "This shell is inside a leftover session profile "
            "(CLAUDE_CONFIG_DIR points at it). Mutating accounts here would "
            "operate on the wrong live store — unset CLAUDE_CONFIG_DIR "
            "or run from a normal shell."
        )

    def _perform_switch(
        self,
        target_account: str,
        emit_output: bool = True,
        force_activate: bool = False,
        provenance: dict | None = None,
        force_if_live_missing: bool = False,
    ) -> dict:
        """Perform the actual account switch with transaction support.

        Returns ``{"from": ref|None, "to": ref, "warnings": [...]}``, capturing the
        left/landed identities under the lock so callers don't reconstruct ``from``
        after the mutation. When ``emit_output`` is False (JSON mode) all human
        output is suppressed — the live-session warning, the "Switched"/"Activated"
        lines, the nested list_accounts() summary and the followup — and the
        live-session warning rides back in ``warnings`` instead.

        ``force_activate`` routes through the direct activation path even when a
        managed live login exists: the stored backup is written over the live
        credentials without backing the live ones up first (post-import recovery
        when the live login is stale).

        The post-switch display runs after the lock releases so that persist
        callbacks inside list_accounts() can re-acquire it.
        """
        from openswap.session import scan_live_sessions

        self._refuse_session_shell()
        warnings_out: list[str] = []
        # Session-mode drift. Switching the default login to an account that
        # also has a live session profile puts the same refresh token in two
        # config dirs — if the server rotates it, one copy goes stale — which
        # is a warning. But once the profile has already rotated past the
        # backup, the backup is a consumed generation and activating it is
        # certain to fail: refuse. With nothing running against the profile
        # the fix is simpler still — adopt its credential into the backup
        # first, and the switch activates the live generation.
        pre_data = self._get_sequence_data() or {}
        pre_account = pre_data.get("accounts", {}).get(target_account, {})
        pre_email = pre_account.get("email", "")
        if pre_email:
            pre_org = pre_account.get("organizationUuid", "") or ""
            sessions, unreadable = scan_live_sessions(
                self._session_dir(target_account, pre_email)
            )
            pids = [s.pid for s in sessions]
            if pids or unreadable:
                if self._session_profile_ahead(target_account, pre_email, pre_org):
                    who = (
                        "a live session-mode Claude instance "
                        f"(PID {', '.join(map(str, pids))})"
                        if pids
                        else f"{unreadable} session record(s) that could not be read"
                    )
                    raise SwitchError(
                        f"Account-{target_account} ({pre_email}) has {who}, and "
                        "its session profile's credential has rotated past the "
                        "stored backup: the backup is a consumed generation, and "
                        "activating it would fail with invalid_grant on its first "
                        "refresh. Exit the session (its credential is adopted into "
                        "the backup once nothing runs against it), or switch to "
                        "another account."
                    )
                if pids:
                    msg = (
                        f"Account-{target_account} ({pre_email}) has a live "
                        "session-mode Claude instance "
                        f"(PID {', '.join(map(str, pids))}). Running the same "
                        "account as both the default login and a session can make "
                        "one copy's token go stale if the server rotates it. If the "
                        "session later fails to authenticate, exit that Claude "
                        "process and use `openswap switch` or the extra."
                    )
                    if emit_output:
                        warning(msg)
                    else:
                        warnings_out.append(msg)
            else:
                self._adopt_session_credential(target_account, pre_email, pre_org)

        # Pre-lock identity resolution (may hit the network — must happen
        # before the locks). Callers that already resolved (self-switch
        # reconciliation) pass it in; force activation never backs up the
        # live credential so it skips the lookup.
        if provenance is None:
            provenance = (
                {"live": None, "resolved": None}
                if force_activate
                else self._prefetch_live_identity()
            )

        # Beyond openswap's own lock, hold Claude Code's advisory locks for the
        # whole mutation (including rollback paths): its token refresh runs
        # under ~/.claude.lock and re-reads credentials there — holding it
        # means a mid-refresh Claude Code either finishes before our swap
        # (backup captures the rotated token) or re-checks after it and aborts.
        # ~/.claude.json.lock likewise keeps the oauthAccount splice from
        # interleaving with Claude Code's own config writes. Everything under
        # here is local I/O — no network while locks are held.
        with FileLock(self.lock_file), claude_credentials_lock(), claude_config_lock():
            data = self._get_sequence_data()
            active_account = data.get("activeAccountNumber")
            current_account = str(active_account) if active_account is not None else None
            target_email = data["accounts"][target_account]["email"]
            to_ref = account_ref(int(target_account), target_email)
            current_identity = self._get_current_account()
            if current_identity is not None:
                current_email, current_org_uuid = current_identity
                current_account = self._find_account_slot(
                    data, current_email, current_org_uuid
                )

            if force_if_live_missing:
                active_now = self._read_active_credentials()
                if active_now.value is None or active_now.keychain_unavailable:
                    raise CredentialReadError(
                        "Cannot verify that live credentials are still missing"
                    )
                live_now = active_now.value or ""
                if looks_like_api_key(live_now) or oauth.extract_access_token(
                    live_now
                ):
                    if current_identity is None:
                        current_ref = None
                    elif current_account is None:
                        current_ref = account_ref(None, current_identity[0])
                    else:
                        current_ref = account_ref(
                            int(current_account), current_identity[0]
                        )
                    return {
                        "from": current_ref,
                        "to": current_ref,
                        "restoreSkipped": True,
                        "warnings": [
                            "Live credentials appeared before restoration; "
                            "the saved backup was not activated."
                        ],
                    }

            config_path = self._get_claude_config_path()

            # Direct activation path: there is no live Claude session yet
            # (e.g. right after import), openswap has no tracked active
            # account yet (e.g. purge -> add-token -> switch-to while a live
            # Claude credential still exists), or --force asked to rewrite the
            # live login from the stored backup. In all cases, skip the
            # back-up-current step: it would either write account-None-*
            # backups or (force) poison the stored backup with stale creds.
            if force_activate or current_identity is None or current_account is None:
                # Account left: None on a fresh machine (no live account at
                # all); an unnumbered ref for an unmanaged live account (slot
                # unknown to openswap); a numbered ref when --force ran with a
                # managed live login.
                if current_identity is None:
                    from_ref = None
                elif current_account is None:
                    from_ref = account_ref(None, current_identity[0])
                else:
                    from_ref = account_ref(int(current_account), current_identity[0])
                target_creds = self._read_target_credentials(
                    target_account, target_email
                )
                target_config = self._read_account_config(target_account, target_email)
                if not target_config:
                    raise SwitchError(
                        f"Account-{target_account} has no stored config backup. "
                        f"Re-add with: openswap --add-account --slot {target_account}"
                    )
                try:
                    target_config_data = json.loads(target_config)
                except json.JSONDecodeError as exc:
                    raise SwitchError(f"Invalid backup config: {exc}")
                target_oauth = target_config_data.get("oauthAccount")
                if not target_oauth:
                    raise SwitchError("Invalid oauthAccount in backup")

                # Snapshot live state so a mid-operation failure can be
                # undone, config identity or not: a wiped or half-written
                # ~/.claude.json can orphan a live credential whose
                # machine-shared MCP state must still reach the composer
                # below (#135) — and the rollback, should activation fail
                # partway. Fail fast when the snapshot is unreadable (None:
                # the credentials file exists but could not be read) rather
                # than overwrite state that has no safety copy; "" means
                # absent in every backend and composes/restores nothing.
                rollback_config_text: str | None = None
                rollback_creds: str | None = self._read_credentials()
                if rollback_creds is None:
                    raise CredentialReadError(
                        "Cannot snapshot live credentials before activation"
                    )
                if current_identity is None:
                    # Fresh machine: normalize "" so the stash, composer, and
                    # rollback all see "nothing to preserve".
                    rollback_creds = rollback_creds or None
                if config_path.exists():
                    try:
                        rollback_config_text = config_path.read_text(
                            encoding="utf-8"
                        )
                    except OSError as e:
                        raise ConfigError(
                            f"Cannot snapshot live config before activation: {e}"
                        )

                # Invariant II (issue #117): this path skips the backup step,
                # so the live credential it replaces would otherwise have no
                # surviving copy — stash it first. For an unmanaged or
                # config-orphaned live login the stash is the only copy
                # anywhere; for --force it guards against the "stale" live
                # login actually being the fresher generation. A failed stash
                # aborts, except under --force where the user explicitly
                # asked for the overwrite.
                if rollback_creds and rollback_creds != target_creds:
                    try:
                        self._stash_live_credential(
                            rollback_creds,
                            "displaced-live-login",
                            current_account or "unmanaged",
                            None,
                        )
                    except Exception as e:
                        if not force_activate:
                            raise SwitchError(
                                "Could not preserve the live credential before "
                                f"activation (safety-copy write failed: {e}); "
                                "aborting rather than destroying it"
                            )
                        msg = (
                            "Could not preserve the replaced live credential "
                            f"(safety-copy write failed: {e}) — proceeding "
                            "because --force explicitly rewrites the live login."
                        )
                        if emit_output:
                            warning(msg)
                        else:
                            warnings_out.append(msg)

                creds_written = False
                config_written = False
                try:
                    self._write_credentials(
                        self._prepare_credentials_for_activation(
                            target_creds, rollback_creds
                        )
                    )
                    creds_written = True

                    # Mirror the normal switch path: preserve existing local
                    # settings/projects when ~/.claude.json already exists, only
                    # swapping in oauthAccount. Fall back to the full imported
                    # config when no usable local config exists.
                    # `_read_json` answers None for ABSENT and for TORN alike,
                    # so a torn ~/.claude.json fell to the else branch and the
                    # 1-key backup config was written over the user's whole
                    # file — measured through the public `switch_to`:
                    # `switched: True` returned with `projects`, `mcpServers`
                    # and `userID` gone.
                    #
                    # Back it up before replacing it, rather than refusing.
                    # Upstream REPLACES a malformed config here on purpose
                    # (`test_clean_switch_fallback_when_local_config_malformed`
                    # — a machine being seeded by import, where the leftover
                    # file is noise), and nothing in scope separates that from
                    # a working install whose config just tore: measured, both
                    # reach this line with `current_account` set and
                    # `_get_current_account()` None. So keep upstream's
                    # behaviour and stop it being LOSSY: the bytes survive next
                    # to the config, named, and the switch still lands.
                    existing_config = (
                        self._read_json(config_path) if config_path.exists() else None
                    )
                    if existing_config is not None:
                        # `is not None`, not truthiness. A VALID but empty `{}`
                        # is readable and loses nothing by being spliced; the
                        # falsy form sent it down the salvage branch and told
                        # the user it "could not be parsed", which is the same
                        # ""-vs-None conflation this branch exists to separate.
                        existing_config["oauthAccount"] = target_oauth
                        self._write_json(config_path, existing_config)
                    else:
                        if config_path.exists():
                            salvage = self._salvage_unreadable(
                                config_path, emit_output, warnings_out
                            )
                            del salvage
                        self._write_json(config_path, target_config_data)
                    config_written = True

                    data["activeAccountNumber"] = int(target_account)
                    data["lastUpdated"] = get_timestamp()
                    self._write_json(self.sequence_file, data)
                except Exception:
                    if config_written and rollback_config_text is not None:
                        try:
                            config_path.write_text(
                                rollback_config_text, encoding="utf-8"
                            )
                            if sys.platform != "win32":
                                os.chmod(config_path, 0o600)
                        except Exception as e:
                            self._logger.error(
                                f"Failed to rollback config: {e}"
                            )
                    if creds_written and rollback_creds is not None:
                        try:
                            self._write_credentials(rollback_creds)
                        except Exception as e:
                            self._logger.error(
                                f"Failed to rollback credentials: {e}"
                            )
                    raise

                if force_activate and current_identity is not None:
                    self._logger.info(
                        f"Activated account {target_account} "
                        "(forced, backup of current login skipped)"
                    )
                else:
                    self._logger.info(
                        f"Activated account {target_account} (no prior live account)"
                    )
                if emit_output:
                    print(
                        f"{accent('Activated')} Account-{target_account} ({target_email})"
                    )
                    print()
                    self._print_switch_followup()
                    print()
                self._replan_new_active(
                    target_account,
                    target_email,
                    data["accounts"][target_account].get("organizationUuid", ""),
                )
                return {"from": from_ref, "to": to_ref, "warnings": warnings_out}

            current_email, _ = current_identity
            from_ref = account_ref(int(current_account), current_email)

            # Create transaction for rollback capability
            try:
                original_creds = self._read_credentials()
                if original_creds is None:
                    raise CredentialReadError("Failed to read current credentials")
                if not original_creds:
                    # An empty read (e.g. a macOS Keychain `security` timeout,
                    # which returns "" rather than raising) must NOT be written
                    # over the departing account's backup — that would destroy
                    # its stored credential. Fail the switch; the backup stays
                    # intact and the caller can retry once the Keychain settles.
                    raise CredentialReadError(
                        "Current account credential is empty (Keychain unreadable?); "
                        "refusing to overwrite its backup"
                    )
                original_config = config_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                raise ConfigError("Claude config file not found")
            except PermissionError:
                raise ConfigError("Permission denied reading Claude config")

            transaction = SwitchTransaction(
                original_credentials=original_creds,
                original_config=original_config,
                original_account_num=current_account,
                original_email=current_email,
                config_path=config_path,
            )

            try:
                # Step 1: Backup current account. Position in ~/.claude.json
                # says which slot is active; only the classification says who
                # owns the live bytes (issue #117: an external write here
                # used to destroy the outgoing slot's refresh token). The
                # identity oracle is strictly advisory — "unresolved" falls
                # back to the exact pre-fix backup, so endpoint state never
                # decides whether a switch completes.
                kind, foreign_slot = self._classify_outgoing_credential(
                    current_account, current_email, original_creds,
                    provenance, data,
                )
                if kind in ("foreign", "alien", "known-foreign"):
                    # Positively not this slot's bytes: never into a slot;
                    # never silently destroyed. The safety copy (which raises
                    # on failure, aborting before the live store is
                    # overwritten) is the license to proceed.
                    self._stash_live_credential(
                        original_creds, kind, current_account,
                        provenance.get("resolved"),
                    )
                    if kind == "foreign":
                        msg = (
                            "Credential ownership mismatch detected. The live "
                            "credential was preserved and was not written "
                            f"into Account-{current_account}. If Account-"
                            f"{foreign_slot} later cannot authenticate, log "
                            "in as it and run: openswap add --slot "
                            f"{foreign_slot}"
                        )
                    elif kind == "known-foreign":
                        msg = (
                            "The live credential was previously identified "
                            "as another account's. It was preserved and not "
                            f"written into Account-{current_account}. If the "
                            "owning account later cannot authenticate, log "
                            "in as it and run: openswap add"
                        )
                    else:
                        msg = (
                            "The live login does not match a managed "
                            "account. It was preserved and not written into "
                            f"Account-{current_account}. If you need that "
                            "account, log in as it and run: openswap add"
                        )
                    if emit_output:
                        warning(msg)
                    else:
                        warnings_out.append(msg)
                elif kind == "foreign-synced":
                    # Another managed account's bytes, and that slot already
                    # holds this lineage — nothing needs preserving, nothing
                    # may be written.
                    msg = (
                        "Credential ownership mismatch detected. The live "
                        f"credential already matches Account-{foreign_slot}'s "
                        "stored backup, so nothing was written into "
                        f"Account-{current_account}."
                    )
                    if emit_output:
                        warning(msg)
                    else:
                        warnings_out.append(msg)
                elif kind == "wiped":
                    # Claude Code emptied the live token fields in place
                    # (its invalid_grant reaction). The blob carries nothing
                    # to preserve and writing it would replace the slot's
                    # only surviving refresh token with empty strings — the
                    # exact destruction chain observed in the field. Config
                    # backup only; the slot's credential backup is the
                    # recovery path.
                    self._write_account_config(
                        current_account, current_email, original_config
                    )
                    msg = (
                        "The live credential's tokens were wiped (Claude "
                        "Code clears them when a refresh is rejected). "
                        f"Account-{current_account}'s stored backup was "
                        "kept. If the account cannot authenticate after "
                        "switching back, log in with Claude Code and run: "
                        "openswap add"
                    )
                    if emit_output:
                        warning(msg)
                    else:
                        warnings_out.append(msg)
                elif kind == "unresolved":
                    # Ownership could not be established (offline, endpoint
                    # failure, malformed response, non-OAuth blob). Fail
                    # open: exact pre-fix backup. Most such divergences are
                    # the account's own rotation — skipping the backup would
                    # leave the slot holding a consumed token — and the
                    # .prev retention inside the write gives even a wrong
                    # call a best-effort recovery cushion. Log only:
                    # indistinguishable from a legitimate rotation, so a
                    # warning would cry wolf.
                    self._write_account_credentials(
                        current_account, current_email, original_creds
                    )
                    self._write_account_config(
                        current_account, current_email, original_config
                    )
                    self._logger.info(
                        f"Backed up account {current_account} (lineage "
                        "differs from the stored backup and ownership could "
                        "not be verified — pre-fix backup)"
                    )
                elif kind == "own-bytes":
                    # Untouched since openswap wrote it — the slot already holds
                    # these bytes. Refresh only the config backup. (Rare since
                    # #145: activation composes live shared MCP state into the
                    # written credential, so live bytes match the slot's only
                    # when nothing was composed in.)
                    self._write_account_config(
                        current_account, current_email, original_config
                    )
                    self._logger.info(
                        f"Backed up account {current_account} (config only; "
                        "credentials unchanged)"
                    )
                else:  # own-family / own-rotated
                    self._write_account_credentials(
                        current_account, current_email, original_creds
                    )
                    self._write_account_config(
                        current_account, current_email, original_config
                    )
                    if kind == "own-rotated":
                        # The profile call proved the identity; backfill a
                        # missing slot uuid (add-token placeholder) while the
                        # sequence file is being rewritten anyway.
                        resolved = provenance.get("resolved") or {}
                        acct = data.get("accounts", {}).get(current_account, {})
                        if not acct.get("uuid") and resolved.get("uuid"):
                            acct["uuid"] = resolved["uuid"]
                    self._logger.info(f"Backed up account {current_account}")

                # Step 2: Retrieve target account
                target_creds = self._read_target_credentials(
                    target_account, target_email
                )
                target_config = self._read_account_config(target_account, target_email)

                if not target_config:
                    raise SwitchError(
                        f"Account-{target_account} has no stored config backup. "
                        f"Re-add with: openswap --add-account --slot {target_account}"
                    )

                # Step 3: Activate target account - credentials
                self._write_credentials(
                    self._prepare_credentials_for_activation(
                        target_creds, original_creds
                    )
                )
                transaction.record_step("credentials_written")
                self._logger.info("Wrote target credentials")

                # Step 4: Update config with target oauthAccount
                target_config_data = json.loads(target_config)
                oauth_section = target_config_data.get("oauthAccount")

                if not oauth_section:
                    raise SwitchError("Invalid oauthAccount in backup")

                # `is not None`, not truthiness — same conflation the direct-
                # activation branch above (:6148-6165) already guards
                # against. A torn ~/.claude.json reads as None here too;
                # `current_config_data["oauthAccount"] = ...` on that None
                # raised `'NoneType' object does not support item
                # assignment` with no salvage copy, losing the user's torn
                # config for good. Absent/unreadable both fall to the same
                # salvage-then-replace the direct-activation branch uses.
                current_config_data = self._read_json(config_path)
                if current_config_data is not None:
                    current_config_data["oauthAccount"] = oauth_section
                    self._write_json(config_path, current_config_data)
                else:
                    if config_path.exists():
                        self._salvage_unreadable(
                            config_path, emit_output, warnings_out
                        )
                    self._write_json(config_path, target_config_data)
                transaction.record_step("config_written")
                self._logger.info("Updated config file")

                # Step 5: Update sequence state
                data["activeAccountNumber"] = int(target_account)
                data["lastUpdated"] = get_timestamp()
                self._write_json(self.sequence_file, data)
                transaction.record_step("sequence_updated")

                self._logger.info(
                    f"Switched from account {current_account} to {target_account}"
                )

            except Exception as e:
                self._logger.error(f"Switch failed: {e}, attempting rollback")
                if transaction.completed_steps:
                    success = transaction.rollback(self)
                    if success:
                        self._logger.info("Rollback successful")
                        raise SwitchError(
                            f"Switch failed and was rolled back: {e}"
                        )
                    else:
                        self._logger.error("Rollback failed!")
                        raise SwitchError(
                            f"Switch failed and rollback also failed: {e}. "
                            f"Manual recovery may be needed."
                        )
                raise

        # Lock released. Safe to do network I/O and let persist callbacks
        # re-acquire the lock from inside list_accounts(). All of this is display
        # only — suppressed in JSON mode (the nested list_accounts() would
        # otherwise leak human output onto the JSON stdout).
        if emit_output:
            print(f"{accent('Switched to')} Account-{target_account} ({target_email})")
            try:
                self.list_accounts()
            except Exception as e:
                self._logger.warning(f"Post-switch usage display failed: {e!r}")
                print(dimmed("  (usage display unavailable — run `openswap --list` to retry)"))
            print()
            self._print_switch_followup()
            print()
        self._replan_new_active(
            target_account,
            target_email,
            data["accounts"][target_account].get("organizationUuid", ""),
        )
        return {"from": from_ref, "to": to_ref, "warnings": warnings_out}

    def _print_switch_followup(self) -> None:
        """Print the note after a successful switch, keyed to where the active
        credential write actually landed.

        A restart is never required: Claude Code clears its cached OAuth token when
        ``.credentials.json`` changes (file storage — effective on the next message)
        or when the macOS Keychain cache TTL (~30s) expires. Both lines are dim
        hints, not warnings; the Keychain line adds that a restart skips the wait.
        The file line also covers macOS when the Keychain was unavailable and the
        switch fell back to the file.
        """
        backend = self._last_active_credentials_backend
        if backend is None:
            # No write happened this run; fall back to the routing hint.
            backend = "keychain" if self._use_keychain() else "file"
        if backend == "keychain":
            print(dimmed(
                "Restart Claude Code to apply immediately — otherwise the "
                "session can take up to ~30 seconds to pick up the new account."
            ))
        else:
            print(dimmed("New account is active on your next message — no restart needed."))
