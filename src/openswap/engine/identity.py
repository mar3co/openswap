"""OpenSwap account engine: (email, organizationUuid) matching and identifier resolution."""

from __future__ import annotations

from openswap.engine.notes import *  # noqa: F403

class IdentityMixin:
    """(email, organizationUuid) matching and identifier resolution."""

    def _validate_email(self, email: str) -> bool:
        """Validate email format."""
        pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        return bool(re.match(pattern, email))

    def resolve_account(self, identifier: str) -> tuple[str, str, str]:
        """Resolve NUM|EMAIL to (account_num, email, organizationUuid).

        Unlike switch_to/remove_account, ambiguity is a hard error rather
        than an interactive prompt: isolated-profile bootstrap (kickoff)
        cannot prompt, so callers need a deterministic resolution.

        Raises:
            AccountNotFoundError: identifier doesn't match any account.
            ConfigError: email matches multiple accounts.
        """
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
        return (
            account_num,
            record.get("email", ""),
            record.get("organizationUuid", "") or "",
        )

    def current_account_number(self) -> str | None:
        """Slot of the live login; ``None`` when there is none or it's unmanaged.

        Deliberately no fallback to the recorded ``activeAccountNumber``: an
        unmanaged live login must return ``None`` — never a guessed slot — so
        the auto-switch engine can't evaluate the wrong account's usage and
        overwrite a login openswap doesn't own (``_perform_switch`` would take
        the no-backup direct-activation path). Use :meth:`has_live_login` to
        tell the two ``None`` cases apart.
        """
        identity = self.live_identity()
        if identity is None:
            return None
        data = self._get_sequence_data() or {}
        email, org_uuid = identity
        return self._find_account_slot(data, email, org_uuid)

    def has_live_login(self) -> bool:
        """Whether ``~/.claude.json`` carries any live account identity."""
        return self.live_identity() is not None

    def live_identity(self) -> tuple[str, str] | None:
        """Current ``(email, organizationUuid)`` from ``~/.claude.json``.

        Public Extra ABI. Same Gmail, two orgs, two identities. ``None`` when
        there is no live login (not a roster ``activeAccountNumber`` guess).
        """
        return self._get_current_account()

    def _get_current_account(self) -> tuple[str, str] | None:
        """Current ``(email, organization_uuid)`` from ``.claude.json``.

        Delegates so there is ONE reader: two copies of this drifted apart
        once already, over whether a null ``accountUuid`` normalises to "".
        """
        triple = self._get_current_identity_triple()
        return None if triple is None else triple[:2]

    def slot_identity(self, num: str | int) -> tuple[str, str] | None:
        """Stored ``(email, organizationUuid)`` for a managed slot, or None."""
        data = self._get_sequence_data() or {}
        acc = (data.get("accounts") or {}).get(str(num)) or {}
        email = acc.get("email") or ""
        if not email:
            return None
        return email, acc.get("organizationUuid") or ""

    def _get_current_identity_triple(
        self, *, strict: bool = False
    ) -> tuple[str, str, str] | None:
        """``(email, org_uuid, account_uuid)`` from ONE read of ``.claude.json``.

        None means no login to capture. With ``strict=True`` an unreadable or
        malformed config raises ``ConfigError`` instead of looking logged out.

        ``add_account`` used to read the config for its identity and again
        near the write. A ``/login`` landing in between pairs one account's
        token with another's metadata -- the exact class
        ``_reject_foreign_credential_capture`` exists to close, so the guard
        must not widen it.
        """
        config_path = self._get_claude_config_path()
        if not config_path.exists():
            return None
        data = self._read_json(config_path, strict=strict)
        if not data:
            return None
        oauth_account = data.get("oauthAccount", {})
        email = oauth_account.get("emailAddress", "")
        if not email:
            return None
        return (
            email,
            oauth_account.get("organizationUuid", "") or "",
            oauth_account.get("accountUuid", "") or "",
        )

    def _live_identity_matches(self, email: str, org_uuid: str) -> bool:
        """Whether the live config identity is (email, org_uuid) right now.

        The under-lock TOCTOU identity re-check shared by the locked refresh
        and the rotated-backup resync: a switch or /login landing between a
        caller's pre-lock read and its lock acquisition changes this identity,
        and a mismatch means the live store is no longer the caller's account
        — nothing there is its to adopt, consume, or overwrite. Compares the
        organization too: two managed slots may share an email across orgs.
        """
        identity = self._get_current_account()
        return identity is not None and identity == (email, org_uuid or "")

    def _resolved_matches_slot_identity(
        self, account_num: str, resolved: dict
    ) -> bool | None:
        """Whether an oracle-resolved identity is this slot's account.

        ``resolved`` is a ``fetch_oauth_profile`` result (non-empty
        ``uuid``; ``email``/``organizationUuid`` possibly None). Uuid-first,
        like ``_classify_outgoing_credential``: uuids are stable where an
        email can be recycled across accounts. Tri-state:

        - True: same account (uuid match with a compatible org, or —
          when the slot has no stored uuid — an exact (email, org) match
          with the resolved org structurally present).
        - False: definitively another account (uuid conflict, a matching
          email under a different org — sibling accounts share emails
          across orgs — or a structurally complete resolved identity
          that matches neither field). Safe to cache.
        - None: unverifiable (slot has no uuid and the resolved identity
          is too partial to condemn or affirm). Must be treated like a
          probe failure — never cached.
        """
        own = self.account_identity(account_num)
        r_uuid = (resolved.get("uuid") or "").strip()
        r_email = resolved.get("email")
        r_org = resolved.get("organizationUuid")
        if r_uuid and own["uuid"]:
            # uuid is globally unique; the org only corroborates, so a
            # missing org on either side is tolerated (mirrors
            # _classify_outgoing_credential's own-rotated check).
            return r_uuid == own["uuid"] and (
                not r_org or not own["organizationUuid"]
                or r_org == own["organizationUuid"]
            )
        # Slot predates uuid tracking (add-token placeholder). Email alone
        # cannot affirm ownership — the same email legitimately exists
        # across personal/org accounts (the reason _live_identity_matches
        # compares the org too). Affirm only an exact (email, org) match
        # with the resolved org structurally present; a missing resolved
        # org is indistinguishable from a personal account, so it is
        # unverifiable — never affirmative, never condemning. On a match,
        # record the resolved uuid so future verdicts are uuid-positive.
        if r_email and r_email == own["email"]:
            if r_org is None:
                return None
            if (r_org or "") == own["organizationUuid"]:
                self.backfill_account_uuid(
                    account_num, r_uuid,
                    expected_email=r_email, expected_org=r_org or "",
                )
                return True
            return False
        if r_email and r_org is not None:
            return False
        return None

    def _lineage_key(
        self, account_num: str, email: str, fingerprint: str
    ) -> tuple[str, str, str, str, str, str]:
        """``_probe_verdicts`` key for a credential lineage, bound to the
        caller's account email AND the slot's full stored identity (email,
        org, uuid): a slot re-created for a different account — same
        number, same email across orgs, even an add-token record whose
        stored email changed while org and uuid stayed blank — must not
        inherit verdicts issued for its predecessor. Any mismatch on any
        component makes the lookup MISS (conservative: re-probe). Built
        fresh at every consult; slot mutations hold the account FileLock,
        so a consult under that lock also revalidates the identity the
        verdict was issued against.

        Accepted identity-model limit, not closed here: a uuid-less,
        org-less record removed and re-added with the SAME email is
        indistinguishable from its predecessor — the (email, org)
        composite IS identity for such records throughout the codebase
        (the switch-path classifier shares the property), so a slot
        generation counter would add state without adding evidence."""
        own = self.account_identity(account_num)
        return (
            account_num, email, own["email"], own["organizationUuid"],
            own["uuid"], fingerprint,
        )

    @staticmethod
    def _find_account_slot(
        data: dict, email: str, organization_uuid: str
    ) -> str | None:
        """Return the slot key for the account matching (email, organizationUuid), else None."""
        for num, account in data.get("accounts", {}).items():
            if (account.get("email") == email and
                    account.get("organizationUuid", "") == organization_uuid):
                return num
        return None

    def _account_exists(self, email: str, organization_uuid: str) -> bool:
        """Check if account exists by (email, organizationUuid) composite key."""
        data = self._get_sequence_data()
        if not data:
            return False
        return self._find_account_slot(data, email, organization_uuid) is not None

    @staticmethod
    def _get_display_tag(email: str, org_name: str, org_uuid: str) -> str:
        """Return display tag for an account's org context."""
        return org_name if org_name else "personal"

    def _resolve_account_identifier(self, identifier: str) -> str | None:
        """Resolve account identifier (number, alias, or email) to account number.

        Resolution precedence: number -> alias -> email.

        Raises:
            ConfigError: if the email matches multiple accounts (ambiguous).
        """
        if identifier.isdigit():
            return identifier

        data = self._get_sequence_data()
        if not data:
            return None

        alias_match = self._find_account_by_alias(identifier)
        if alias_match is not None:
            return alias_match

        matches = [
            num for num, account in data.get("accounts", {}).items()
            if account.get("email") == identifier
        ]

        if len(matches) == 0:
            return None
        if len(matches) == 1:
            return matches[0]

        details = ", ".join(
            f"{num} [{data['accounts'][num].get('organizationName') or 'personal'}]"
            for num in matches
        )
        raise ConfigError(
            f"Email '{identifier}' is ambiguous — matches accounts: {details}. "
            f"Use account number instead (e.g., openswap --switch-to 1)."
        )

    def _live_matches_slot_backup(self, slot: str, email: str) -> bool:
        """Whether the live credential is provably the slot's stored lineage.

        Byte or refresh-token-fingerprint equality against the slot's backup.
        Used to make self-switch short-circuits provenance-aware: a no-op is
        only safe when live state matches what the slot holds — when they
        have diverged, the switch should run so ``_perform_switch`` can
        classify the live bytes (re-sync or preserve) instead of silently
        leaving the divergence in place. Unreadable/empty live credentials
        return True (keep the no-op: forcing a switch on missing evidence
        would fail later anyway).
        """
        try:
            live = self._read_credentials()
        except Exception:
            return True
        if not live:
            return True
        backup = self._read_account_credentials(slot, email)
        if not backup:
            return False
        return live == backup or (
            oauth.credential_fingerprint(live)
            == oauth.credential_fingerprint(backup)
        )
