"""OpenSwap account engine: Claude Code live credential store: Keychain/file read-write, locks, degraded vs empty."""

from __future__ import annotations

from openswap.engine.notes import *  # noqa: F403

class LiveMixin:
    """Claude Code live credential store: Keychain/file read-write, locks, degraded vs empty."""

    def _get_claude_config_path(self) -> Path:
        """Get the Claude configuration file path, mirroring claude-code."""
        return get_global_config_path()

    @property
    def _keychain_usable_cache(self) -> bool | None:
        return self._store._keychain_usable_cache

    @_keychain_usable_cache.setter
    def _keychain_usable_cache(self, value: bool | None) -> None:
        self._store._keychain_usable_cache = value

    @property
    def _keychain_disabled_until(self) -> float:
        return self._store._keychain_disabled_until

    @_keychain_disabled_until.setter
    def _keychain_disabled_until(self, value: float) -> None:
        self._store._keychain_disabled_until = value

    @property
    def _last_active_credentials_backend(self) -> str | None:
        return self._store._last_active_credentials_backend

    @_last_active_credentials_backend.setter
    def _last_active_credentials_backend(self, value: str | None) -> None:
        self._store._last_active_credentials_backend = value

    def _kc_call(self, fn, *args):
        return self._store._kc_call(fn, *args)

    def _use_keychain(self) -> bool:
        return self._store._use_keychain()

    def _read_credentials(self) -> str | None:
        return self._store._read_credentials()

    def _read_active_credentials(self) -> ActiveCredentials:
        return self._store._read_active_credentials()

    def _refuse_degraded_capture(self) -> str | None:
        """Refuse to CAPTURE bytes a degraded read produced.

        Both env-var branches of :meth:`_read_capture_credentials` pass
        ``strict_keychain=True`` — an unreadable (not absent) Keychain raises
        rather than silently capturing the plaintext seed, which on macOS may
        be the consumed predecessor because Claude Code rotates keychain-only.

        The DEFAULT path is the most-used one and had no such guard: it reads
        through ``_read_credentials``, which is ``_read_active_credentials()``
        with ``degraded`` discarded. So a locked-keychain ``openswap add``
        captured the possibly-spent fallback into the slot backup, and
        ``add_account`` then cleared the dead-token strike — re-creating on the
        common path exactly the stale-consume this PR exists to prevent.

        I-1 (round 9): returns the value THIS read produced so the caller
        captures those exact bytes instead of reading again. The check-read
        and a separate use-read are two independent Keychain reads — a
        Keychain that answers the first and fails the second passes the
        guard and then captures the possibly-stale plaintext fallback
        anyway, which is precisely the outcome this guard exists to prevent.
        """
        active = self._read_active_credentials()
        if active.degraded:
            raise CredentialReadError(
                "The macOS Keychain is unreadable right now (locked or no GUI "
                "session), so the only readable credential is a plaintext "
                "fallback that may be a superseded generation — capturing it "
                "would file a spent refresh token against this slot. Retry "
                "from a GUI terminal."
            )
        return active.value

    def _read_capture_credentials(self) -> str | None:
        """Read the credential of the profile the environment points at.

        ``add_account`` fills one slot from two reads: the identity out of
        ``.claude.json`` and the credential out of the active store. The active
        store's file backend follows ``CLAUDE_CONFIG_DIR`` (through
        ``get_claude_config_home``), but its macOS Keychain backend is pinned to
        the unsuffixed ``CLAUDE_CODE_KEYCHAIN_SERVICE``. So on macOS the two
        reads land in different profiles and the slot ends up holding one
        account's email against another account's token.

        Read the OAuth credential the way claude resolves it for the same
        environment, so the slot's email and token come from one profile.
        Claude (2.1.220 ``getMacOsKeychainStorageServiceName``/storage-path
        resolution) sources secure storage from ``CLAUDE_SECURESTORAGE_CONFIG_DIR``
        when that is *defined*, else ``CLAUDE_CONFIG_DIR`` — with defined-but-empty
        meaning the *default secure store* (unsuffixed Keychain item,
        ``~/.claude/.credentials.json``). Not the active store: its file backend
        follows ``CLAUDE_CONFIG_DIR``, which may point elsewhere. Identity stays
        on ``CLAUDE_CONFIG_DIR`` either way; only the credential read moves.

        Two fallbacks stay inside that profile. An env var naming the default
        profile means the active store, since a user exporting
        ``CLAUDE_CONFIG_DIR=~/.claude`` may only have the unsuffixed item.
        A managed API key sits outside any OAuth store, and
        :meth:`_reject_live_api_key_capture` still has to answer for one.

        Strict on the keychain: an unreadable (not absent) entry raises
        :class:`CredentialReadError` rather than silently capturing the
        profile's possibly-stale plaintext seed — and rather than reaching the
        fallbacks below, which belong to other stores entirely.

        Read-only. openswap does not write claude's hashed keychain entry — see
        the ``session`` module docstring for why.
        """
        from openswap.session import read_config_dir_credentials

        secure_env = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR")

        if secure_env is not None:
            # A defined override names the *only* store claude will read for
            # this environment — defined-but-empty pins the default profile
            # (unsuffixed keychain item, ``~/.claude/.credentials.json``). On
            # a miss claude sees a logged-out environment, so no falling back
            # into the active store: its file backend follows
            # ``CLAUDE_CONFIG_DIR``, and with the two vars diverged that would
            # capture a profile claude is not reading (cross-profile leak).
            creds = read_config_dir_credentials(
                secure_env or str(get_default_claude_config_home()),
                strict_keychain=True,
                keychain_service=CLAUDE_CODE_KEYCHAIN_SERVICE if not secure_env else None,
            )
            if creds:
                return creds
            # The tail below is not a continuation of this branch: it reads
            # ``primaryApiKey`` through ``get_global_config_path()``, which
            # follows ``CLAUDE_CONFIG_DIR``. With the two vars diverged that
            # is the cross-profile capture this branch just refused — and it
            # cannot be reached by the secure profile's OWN key either, since
            # ``read_config_dir_credentials`` is OAuth-only and never looks at
            # ``primaryApiKey``. A miss here is what claude sees: logged out.
            return ""
        elif not config_dir:
            return self._refuse_degraded_capture()
        else:
            creds = read_config_dir_credentials(config_dir, strict_keychain=True)
            if creds:
                return creds
            if _same_directory(Path(config_dir), get_default_claude_config_home()):
                # Safe only on this legacy path: the active store's env-following
                # file backend and the default profile coincide here.
                return self._refuse_degraded_capture()
        # Only this profile's own ``primaryApiKey`` — never the unsuffixed
        # "Claude Code" Keychain item, which belongs to the default profile
        # and would answer for a login that is not the one being added.
        key = (self._read_json(get_global_config_path()) or {}).get("primaryApiKey")
        return key if isinstance(key, str) else ""

    def _write_credentials(self, credentials: str) -> None:
        self._store._write_credentials(credentials)

    def has_live_credentials(self) -> bool:
        """Whether Claude's active store currently has usable credentials.

        Unlike :meth:`has_live_login`, this checks the credential bytes, not
        merely the config identity. An unreadable store is never treated as
        empty: callers must not overwrite state they could not inspect.
        """
        active = self._read_active_credentials()
        if active.value is None or active.keychain_unavailable:
            raise CredentialReadError("Cannot safely read live Claude credentials")
        value = active.value or ""
        return looks_like_api_key(value) or bool(oauth.extract_access_token(value))

    def _record_active_verdict(self, active) -> None:
        """Record THIS thread's active-read verdict (see `_active_verdict_tls`)."""
        self._active_verdict_tls.value = active

    def _record_active_backup_fallback(self, enabled: bool) -> None:
        """Record that this pass is reading active usage from its backup."""
        self._active_verdict_tls.backup_fallback = {
            "enabled": bool(enabled),
            "repaired": False,
        }

    def _active_backup_fallback(self) -> bool:
        state = getattr(self._active_verdict_tls, "backup_fallback", None)
        return bool(state and state.get("enabled"))

    def _mark_active_backup_repaired(self) -> None:
        state = getattr(self._active_verdict_tls, "backup_fallback", None)
        if state is not None:
            state["repaired"] = True

    def _active_backup_repaired(self) -> bool:
        state = getattr(self._active_verdict_tls, "backup_fallback", None)
        return bool(state and state.get("repaired"))

    def _with_active_verdict(self, fn):
        """Wrap `fn` so a worker thread inherits THIS thread's verdict.

        Thread-local keeps two concurrent lanes from erasing each other's, but
        `_fetch_active_usage` always runs on a pool worker that never read —
        measured 30/30 verdicts lost, and the consume gate never fired.
        """
        verdict = self._active_verdict()
        backup_fallback = getattr(
            self._active_verdict_tls, "backup_fallback", None
        )

        def _inherit(*args, **kwargs):
            self._record_active_verdict(verdict)
            # Share this pass-local state object with the worker. The worker
            # can mark a successful repair and the collector thread then sees
            # it without leaking the verdict into a concurrent GUI lane.
            self._active_verdict_tls.backup_fallback = backup_fallback
            return fn(*args, **kwargs)

        return _inherit

    def _active_verdict(self):
        """This thread's active-read verdict; a clean one if it never read."""
        from openswap.credentials import ActiveCredentials

        return getattr(self._active_verdict_tls, "value", None) or ActiveCredentials(
            "", False, False
        )

    @property
    def _active_keychain_unavailable(self) -> bool:
        return self._active_verdict().keychain_unavailable

    @property
    def _active_read_unreadable(self) -> bool:
        """Whether THIS thread's active-credential read outright FAILED
        (plaintext-file OSError), as opposed to a genuinely absent slot.

        ``keychain_unavailable`` alone misses this off macOS:
        ``_read_active_credentials``'s file-read-error arm returns
        ``ActiveCredentials(None, keychain_failed, keychain_failed)``, and
        ``keychain_failed`` stays False on Linux/WSL/Windows (there is no
        Keychain to fail there) — ``value is None`` is the only surviving
        signal of the three states (readable / genuinely absent / could not
        be read). Mirrors the ``is None`` guard ``_slot_token_dead`` already
        uses for the same tri-state on the backup side.
        """
        return self._active_verdict().value is None

    @property
    def _active_read_degraded(self) -> bool:
        return self._active_verdict().degraded

    def live_credential_owner(self, num: str | int) -> dict:
        """Whose login the live credential is, relative to slot ``num``.

        ``{"state": ...}`` where state is ``"matches"`` (the live bytes are
        the slot's own backup lineage), ``"own"`` (rotated, but resolved to
        this slot), ``"other"`` (another account: ``email`` and, when it is
        a saved account, ``slot``) or ``"unknown"`` (couldn't be resolved).
        Uuid-first matching, like the switch classifier. Makes a network
        call; never call it while holding a lock.
        """
        num = str(num)
        email = (self.slot_identity(num) or ("",))[0]
        if self._live_matches_slot_backup(num, email):
            return {"state": "matches"}
        resolved = self._prefetch_live_identity()["resolved"]
        if resolved is None:
            return {"state": "unknown"}
        accounts = (self._get_sequence_data() or {}).get("accounts") or {}
        owner = next(
            (
                str(slot) for slot in accounts
                if self._resolved_matches_slot_identity(str(slot), resolved)
            ),
            None,
        )
        if owner == num:
            return {"state": "own"}
        return {"state": "other", "email": resolved.get("email"), "slot": owner}

    def _prefetch_live_identity(self) -> dict:
        """Resolve the live credential's owner BEFORE the locks are taken.

        The switch-time backup copies live credential bytes into the slot named
        by ``~/.claude.json`` — two files with independent writers. When they
        agree (bytes or refresh-token lineage match the slot's stored backup)
        no network is needed. When they diverge, only the API can say whose
        token the live bytes are (the credential blob carries no identity), and
        "no network while locks are held" forces that call to happen here.

        Returns ``{"live": str|None, "resolved": dict|None}``. ``resolved`` is
        only trustworthy while the live bytes haven't moved — the under-lock
        classifier re-checks byte equality before using it.
        """
        result: dict = {"live": None, "resolved": None}
        try:
            live = self._read_credentials()
        except Exception as e:
            self._logger.debug(f"Pre-lock live credential read failed: {e!r}")
            return result
        result["live"] = live
        if not live:
            return result
        identity = self._get_current_account()
        if identity is None:
            return result
        data = self._get_sequence_data() or {}
        slot = self._find_account_slot(data, identity[0], identity[1])
        if slot is None:
            return result
        backup = self._read_account_credentials(slot, identity[0])
        if backup == live or (
            oauth.credential_fingerprint(backup)
            == oauth.credential_fingerprint(live)
        ):
            return result  # provenance already established locally
        access_token = oauth.extract_access_token(live)
        if not access_token:
            return result  # raw API key / garbled JSON — nothing to resolve
        try:
            result["resolved"] = oauth.fetch_oauth_profile(access_token)
        except Exception as e:
            # fetch_oauth_profile swallows its own failures; this belt keeps
            # the invariant structural — the oracle is advisory and must
            # never fail a switch.
            self._logger.debug(f"Profile resolution raised: {e!r}")
        return result
