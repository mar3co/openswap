"""Public in-process OpenSwap account engine."""

from __future__ import annotations

from contextlib import ExitStack

from openswap.engine.notes import *  # noqa: F403
from openswap.engine.consume import ConsumeMixin
from openswap.engine.freshen import FreshenMixin
from openswap.engine.identity import IdentityMixin
from openswap.engine.live import LiveMixin
from openswap.engine.session_profile import SessionProfileMixin
from openswap.engine.slots import SlotsMixin
from openswap.engine.snapshot import SnapshotMixin
from openswap.engine.switch import SwitchMixin
from openswap.worker.leases import AccountLeaseStore


class Engine(
    LiveMixin,
    SlotsMixin,
    IdentityMixin,
    ConsumeMixin,
    SwitchMixin,
    SessionProfileMixin,
    SnapshotMixin,
    FreshenMixin,
):
    """Multi-account switcher for Claude Code. Extra, autoswitch, kickoff, and CLI call this."""

    provider = "claude"

    def __init__(self, debug: bool = False):
        self.home = Path.home()
        self.platform = Platform.detect()
        self.backup_dir = get_backup_root()

        # Migrate legacy ~/.claude-swap-backup to the new XDG path on Linux/WSL
        # before any logger or directory setup writes to the new location.
        # Migration is a no-op on macOS/Windows where backup_dir already
        # equals the legacy path. MigrationError on a genuine collision
        # propagates as a ClaudeSwitchError and is caught by the CLI.
        if migrate_legacy_backup_dir(self.backup_dir):
            legacy = get_legacy_backup_root()
            print(
                f"openswap: migrated data from {legacy} to {self.backup_dir}",
                file=sys.stderr,
            )

        self.sequence_file = self.backup_dir / "sequence.json"
        self.configs_dir = self.backup_dir / "configs"
        self.credentials_dir = self.backup_dir / "credentials"
        self.lock_file = self.backup_dir / ".lock"
        self._logger = setup_logging(self.backup_dir, debug=debug)
        self._usage_store = UsageStore(self.backup_dir / "cache")
        # (settings mtime, (threshold, models)) — see _poll_policy_inputs.
        self._poll_inputs_cache: tuple[float | None, tuple[float, tuple[str, ...]]] | None = None
        self._poll_inputs_override: tuple[float, tuple[str, ...]] | None = None

        # The credential storage layer (active + per-account backup stores, macOS
        # Keychain-vs-file routing, the per-process capability cache). Reads its
        # live config (platform, _logger, credentials_dir) back off this switcher.
        # Constructed BEFORE run_migrations(), which performs storage ops on macOS.
        # One store per switcher: the capability cache is per-process.
        self._store = CredentialStore(self)

        # The active read's verdict, PER THREAD. Set by _build_accounts_info
        # from the active slot's own read; consumed later by the usage
        # sentinel, the rotation resync and the consume gate.
        #
        # Thread-local: a fact about one READ, and a GUI shell can run two
        # lanes on one switcher (store refresh while a normal one is in
        # flight). A build's unconditional reset erased the other lane's
        # verdict — measured against a 4ms window, 60 of 60 lost, after which
        # the consume gate POSTs a possibly-spent grant.
        self._active_verdict_tls = threading.local()

        # Accounts already warned about a provenance problem with the active
        # credential — each condition persists across collect passes and
        # would otherwise log every tick. Cleared when its condition clears.
        # Keyed by (slot, email, reason) so a slot reused for a different
        # account in a long-lived process warns afresh and distinct
        # conditions don't suppress each other's warning.
        self._provenance_warned: set[tuple[str, str, str]] = set()

        # Definitive ownership verdicts for credential lineages, keyed by
        # _lineage_key (slot, caller email, stored email, org, uuid,
        # refresh-lineage fingerprint — the full slot identity, so a slot
        # re-created for a different account never inherits its
        # predecessor's verdicts):
        # True when the profile oracle resolved the lineage to the slot's
        # identity (or we produced it ourselves with a refresh POST), False
        # when it resolved to a foreign identity. Probe failures and
        # unverifiable results are never cached — a False on partial
        # evidence would permanently block a legitimate resync in a
        # long-lived process. In-memory only; the locked refresh paths
        # consult it because network under locks is forbidden, and it keeps
        # a persistent drift state from re-probing the profile endpoint
        # every collect pass.
        self._probe_verdicts: dict[
            tuple[str, str, str, str, str, str], bool
        ] = {}

        # Run any pending one-time data migrations (e.g. relocating Windows
        # backup credentials out of Credential Manager into files). Imported
        # lazily to avoid a circular import, and self-contained so it never
        # aborts construction. No-op on fresh installs / once recorded.
        from openswap.migrations import run_migrations

        run_migrations(self)

    def _is_running_in_container(self) -> bool:
        """Check if running inside a container."""
        # Check environment variables (works on all platforms)
        if os.environ.get("CONTAINER") or os.environ.get("container"):
            return True

        # Windows doesn't have the same container indicators
        if self.platform == Platform.WINDOWS:
            return False

        # Check for Docker environment file (Linux/macOS)
        if Path("/.dockerenv").exists():
            return True

        # Check cgroup for container indicators (Linux)
        cgroup_path = Path("/proc/1/cgroup")
        if cgroup_path.exists():
            try:
                content = cgroup_path.read_text()
                if any(
                    x in content
                    for x in ["docker", "lxc", "containerd", "kubepods"]
                ):
                    return True
            except PermissionError:
                pass

        # Check mount info (Linux)
        mountinfo_path = Path("/proc/self/mountinfo")
        if mountinfo_path.exists():
            try:
                content = mountinfo_path.read_text()
                if any(x in content for x in ["docker", "overlay"]):
                    return True
            except PermissionError:
                pass

        return False

    def _read_json(self, path: Path, *, strict: bool = False) -> dict | None:
        """Read and parse a JSON file. None when the file is ABSENT.

        With ``strict=True``, raises ``ConfigError`` when the file is THERE
        but unreadable — the distinction ``_read_global_config``'s callers
        keep having to make, and the one ~25 ``or {}`` call sites here were
        silently collapsing. Default False so the reader stays a reader:
        upstream's import path DELIBERATELY replaces a malformed config it is
        about to seed (`test_clean_switch_fallback_when_local_config_malformed`),
        and a blanket refusal would flip that intent.

        Measured on the plain switch path: a torn ``~/.claude.json`` read as
        None, fell to the `else` branch at :5954, and the 1-key backup config
        was written over the user's whole file — `projects`, `mcpServers`,
        `userID` gone, `switched: True` returned. Absent is a genuine empty
        start; unreadable is a file we must not overwrite unread.

        Also rejects a non-dict payload. ``json.loads`` happily returns a str
        or an int for a file holding `"hello"` or `123`, and every caller then
        fails on `.get` with a raw AttributeError that escapes
        ``ClaudeSwitchError``. ``_read_global_config`` already ends with the
        same isinstance check; the two readers of the same file disagreed.
        """
        if not path.exists():
            return None
        try:
            data = json.loads(read_text_with_retry(path))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._logger.warning(f"Invalid JSON in {path}")
            if strict:
                raise ConfigError(
                    f"{path} exists but could not be parsed ({e}). Repair or "
                    "move it, then retry — refusing to overwrite it unread."
                ) from e
            return None
        except OSError as e:
            self._logger.warning(f"Could not read {path}: {e}")
            if strict:
                raise ConfigError(
                    f"{path} exists but could not be read ({e}). Fix what is "
                    "blocking the read, then retry."
                ) from e
            return None
        if not isinstance(data, dict):
            self._logger.warning(
                f"{path} holds {type(data).__name__}, not a JSON object"
            )
            if strict:
                raise ConfigError(
                    f"{path} holds {type(data).__name__}, not a JSON object. "
                    "Repair or move it, then retry."
                )
            return None
        return data

    def _salvage_unreadable(
        self, path: Path, emit_output: bool, warnings_out: list[str]
    ) -> Path:
        """Copy an unreadable file aside before it is replaced. Returns the copy.

        Three things the first cut got wrong, all of them the same promise —
        THE BYTES SURVIVE AND THE USER KNOWS:

        MODE. `shutil.copy2` preserves the source mode. Measured on a 0644
        `~/.claude.json` holding `primaryApiKey`: the replacement got 0600 from
        `_write_json` and the salvage stayed 0644, so the secret ended up
        world-readable in a file openswap created. Copied without metadata and
        chmod'ed 0600 explicitly.

        COLLISION. The stamp is second-resolution and `copy2` onto an existing
        name overwrites. Two failed switches inside one second left ONE file —
        measured, the first user's data unrecoverable. The retry is exactly
        what a user does next, so the guard lost the bytes precisely when it
        was needed. A counter suffix makes each copy its own file.

        NAME. The first cut stamped with `get_timestamp()`, whose ISO form
        carries `:` — forbidden in a Windows filename. Measured on CI (run
        30774451162): five tests died with `[Errno 22] Invalid argument`, the
        copy raised, and the switch ABORTED, which is worse than the data loss
        this guard exists to prevent. `int(time.time())` is what
        `credentials.py`'s sibling `.corrupt-` aside already uses; reusing it
        keeps one convention rather than inventing a third.

        VISIBILITY. `warnings_out` is only rendered by the JSON envelope. In
        human mode the user saw "Activated Account-1" and nothing else while
        their `projects`/`mcpServers` were gone from the live config — every
        other `warnings_out.append` in `_perform_switch` is paired with an
        `if emit_output: warning(msg)`; this one was not.
        """
        stem = f"{path.name}.unreadable-{int(time.time())}"
        salvage = path.with_name(stem)
        n = 1
        while salvage.exists():
            salvage = path.with_name(f"{stem}.{n}")
            n += 1
        try:
            shutil.copy(path, salvage)          # NOT copy2: mode is set below
            if sys.platform != "win32":
                os.chmod(salvage, 0o600)
        except OSError as e:
            raise SwitchError(
                f"{path} could not be parsed and the salvage copy failed "
                f"({e}); aborting rather than destroying it"
            )
        msg = (
            f"{path.name} could not be parsed — a copy was kept at "
            f"{salvage.name}"
        )
        self._logger.warning(f"{path} could not be parsed; a copy was kept at "
                             f"{salvage} before it was replaced")
        warnings_out.append(msg)
        if emit_output:
            warning(msg)
        return salvage

    def _write_json(self, path: Path, data: dict) -> None:
        """Write JSON file with validation."""
        content = json.dumps(data, indent=2)

        # Write to temp file first
        temp_path = path.with_suffix(f".{os.getpid()}.tmp")
        temp_path.write_text(content, encoding="utf-8")

        # Validate written content
        try:
            json.loads(temp_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            temp_path.unlink()
            raise ConfigError("Generated invalid JSON")

        # Permissions go on the temp file so the rename below is the final,
        # atomic commit: nothing can fail after the file is published (a
        # chmod on the final path could raise with the write already live,
        # making callers roll back around committed metadata).
        if sys.platform != "win32":
            os.chmod(temp_path, 0o600)
        shutil.move(str(temp_path), str(path))

    def purge(self) -> None:
        """Remove all traces of openswap from the system.

        This removes:
        - All stored account credentials (``.enc`` files on Linux/WSL/Windows; on
          macOS both the Keychain items via ``security`` and any fallback ``.enc``
          files), plus a best-effort sweep of any pre-migration keyring / Windows
          Credential Manager entries left behind
        - All managed data under the active backup directory; Claude and
          Codex provider locks, the worker lifecycle lock and the unleased
          kickoff locks remain as empty concurrency anchors
        - Any stale legacy ~/.claude-swap-backup directory left around from
          before the XDG migration
        """
        self._refuse_session_shell()
        legacy = get_legacy_backup_root()
        provider_lock_paths = (
            self.backup_dir / ".lock",
            self.backup_dir / "codex" / ".lock",
            self.backup_dir / ".settings.lock",
            self.backup_dir / "worker" / "lifecycle.lock",
            self.backup_dir / "worker" / "leases" / "claude.unleased.lock",
            self.backup_dir / "worker" / "leases" / "codex.unleased.lock",
        )
        legacy_distinct = legacy != self.backup_dir and not any(
            _path_is_within(lock_path, legacy) for lock_path in provider_lock_paths
        )

        # Refuse while any session-mode claude is running: purging would pull
        # its profile (and keychain entry) out from under a live process.
        sessions_root = self.backup_dir / "sessions"
        session_dirs = (
            [d for d in sessions_root.iterdir() if d.is_dir()]
            if sessions_root.is_dir()
            else []
        )
        from openswap.session import scan_live_sessions

        live = {}
        unreadable = {}
        for d in session_dirs:
            sessions, bad = scan_live_sessions(d)
            if sessions:
                live[d.name] = [s.pid for s in sessions]
            elif bad:
                unreadable[d.name] = bad
        if live:
            details = "; ".join(
                f"{name} (PID {', '.join(map(str, pids))})"
                for name, pids in live.items()
            )
            raise SessionError(
                f"Live session-mode Claude instance(s) found: {details}. "
                "Exit them first, then retry --purge."
            )
        if unreadable:
            details = "; ".join(
                f"{name} ({n} record(s))" for name, n in unreadable.items()
            )
            raise SessionError(
                f"Session records that could not be read: {details}. Whether a "
                "Claude instance is live cannot be determined, and purging "
                "would pull a live profile out from under it. Repair or remove "
                "them, then retry --purge."
            )

        warning("This will remove ALL openswap data from your system:")
        print(f"  - Backup directory: {self.backup_dir}")
        if legacy_distinct and legacy.exists():
            print(f"  - Legacy backup directory: {legacy}")
        if self.platform == Platform.MACOS:
            print("  - All stored account credentials (macOS Keychain and/or files)")
        else:
            print("  - All stored account credential files")
        if session_dirs:
            print("  - All session profiles and their Keychain entries")
        print()
        print(dimmed("Note: This does NOT affect your current Claude Code login."))
        print()

        confirm = input("Are you sure you want to purge all data? [y/N] ")
        if confirm.lower() != "y":
            print(dimmed("Cancelled"))
            return

        # Confirmation stays outside the locks. Hold the lifecycle lock across
        # status checks and deletion so a concurrent enable/run cannot start a
        # worker while the journal is removed. Purge never unloads a managed
        # worker automatically; an installed service must be explicitly
        # disabled before retrying.
        stores = [
            AccountLeaseStore(self.backup_dir, provider)
            for provider in ("claude", "codex")
        ]
        codex_dir = self.backup_dir / "codex"
        worker_dir = self.backup_dir / "worker"
        settings_lock_path = self.backup_dir / ".settings.lock"
        lifecycle_path = worker_dir / "lifecycle.lock"
        if (
            self.backup_dir.is_symlink()
            or codex_dir.is_symlink()
            or worker_dir.is_symlink()
            or (self.backup_dir / ".lock").is_symlink()
            or (codex_dir / ".lock").is_symlink()
            or settings_lock_path.is_symlink()
            or (settings_lock_path.exists() and not settings_lock_path.is_file())
            or lifecycle_path.is_symlink()
        ):
            raise SessionError(
                "A worker, provider, or settings lock path is unsafe; refusing "
                "to purge account and lease state."
            )
        from openswap.worker.cli import lifecycle_lock
        from openswap.settings import _settings_write_lock

        with lifecycle_lock(self.backup_dir):
            # Use the canonical settings lock after the lifecycle lock and
            # before provider locks. This serializes purge with every settings
            # writer and keeps the lock inode stable while settings.json is
            # removed, so a waiting writer cannot proceed through an unlinked
            # lock and race a replacement lock.
            with _settings_write_lock(self.backup_dir):
                # The global provider-lock order (Codex, then Claude) that
                # the worker and the account commands also use; sorting by
                # lock path put Claude's <backup>/.lock first and could
                # deadlock against them.
                from openswap.worker.leases import provider_lock_rank

                stores.sort(key=provider_lock_rank)
                # Refuse active/uncertain leases before worker checks; release
                # provider locks before status IPC, then recheck before delete.
                with ExitStack() as stack:
                    for store in stores:
                        guard = stack.enter_context(store.mutation_guard())
                        guard.assert_available()
                self._refuse_worker_restart_locked()
                with ExitStack() as stack:
                    for store in stores:
                        guard = stack.enter_context(store.mutation_guard())
                        guard.assert_available()
                    # A kickoff with Remote tasks off holds only its unleased-run
                    # lock: refuse while one runs, and hold both so none starts.
                    for store in stores:
                        if not stack.enter_context(store.unleased_run(timeout=0)):
                            raise SessionError(
                                "A scheduled kickoff is running; retry purge when it finishes."
                            )
                    self._purge_confirmed(legacy, legacy_distinct, session_dirs)

    def _refuse_worker_restart_locked(self) -> None:
        """Require an explicitly disabled and stopped worker before purge."""
        import json

        from openswap.settings import settings_path
        from openswap.worker.cli import _snapshot

        try:
            snapshot = _snapshot(self.backup_dir)
            policy_path = settings_path(self.backup_dir)
            raw_settings = (
                json.loads(policy_path.read_text(encoding="utf-8"))
                if policy_path.exists()
                else {}
            )
        except Exception:
            raise SessionError(
                "Worker status is unavailable. Run `openswap worker disable`, "
                "stop any manual worker, and retry purge."
            ) from None
        if not isinstance(raw_settings, dict):
            raise SessionError(
                "Worker policy is unresolved. Run `openswap worker disable` "
                "and retry purge."
            )
        raw_worker = raw_settings.get("worker", {})
        if not isinstance(raw_worker, dict):
            raise SessionError(
                "Worker policy is unresolved. Run `openswap worker disable` "
                "and retry purge."
            )
        enabled = raw_worker.get("enabled", False)
        if type(enabled) is not bool:
            raise SessionError(
                "Worker policy is unresolved. Run `openswap worker disable` "
                "and retry purge."
            )
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("process_state") != "stopped"
            or snapshot.get("active_job") is not None
            or snapshot.get("lease_quarantined") is not False
        ):
            raise SessionError(
                "Worker is running or its state is unresolved. Run `openswap "
                "worker disable`, stop any manual worker, and retry purge."
            )
        if enabled:
            raise SessionError(
                "Remote tasks worker is enabled. Run `openswap worker disable` "
                "and retry purge."
            )
        if self.platform == Platform.MACOS:
            from openswap.worker.launch_agent import status as worker_service_status

            try:
                service = worker_service_status()
            except Exception:
                raise SessionError(
                    "Worker service status is unavailable. Run `openswap "
                    "worker disable` and retry purge."
                ) from None
            if (
                not isinstance(service, dict)
                or type(service.get("loaded")) is not bool
                or type(service.get("installed")) is not bool
            ):
                raise SessionError(
                    "Worker service status is unresolved. Run `openswap "
                    "worker disable` and retry purge."
                )
            if service["loaded"] or service["installed"]:
                raise SessionError(
                    "Remote tasks worker is installed or loaded. Run "
                    "`openswap worker disable` and retry purge."
                )

        # A stopped snapshot alone is not proof that a manually started worker
        # released its singleton lock. The lifecycle lock excludes new starts.
        instance_path = self.backup_dir / "worker" / "instance.lock"
        if instance_path.is_symlink():
            raise SessionError("Worker process lock is unsafe; refusing to purge.")
        try:
            with FileLock(instance_path, timeout=0):
                pass
        except Exception:
            raise SessionError(
                "Worker process may still be running. Run `openswap worker "
                "disable`, stop any manual worker, and retry purge."
            ) from None

    def _purge_confirmed(
        self, legacy: Path, legacy_distinct: bool, session_dirs: list[Path]
    ) -> None:
        """Delete purge targets while settings and provider locks are held."""
        removed_items = []

        # Remove credentials. On macOS backups may be in the Keychain and/or .enc
        # files (auto-fallback), so clean both; Linux/WSL/Windows are file-only.
        data = self._get_sequence_data()
        if data:
            for account_num, account_info in data.get("accounts", {}).items():
                email = account_info.get("email", "")
                nums = [account_num]
                if str(account_num) != "None":
                    nums.append("None")
                usernames = [f"account-{num}-{email}" for num in nums]

                # .enc files (Linux/WSL/Windows always; macOS fallback copies).
                for num in nums:
                    cred_file = self.credentials_dir / f".creds-{num}-{email}.enc"
                    try:
                        if cred_file.exists():
                            cred_file.unlink()
                            removed_items.append(f"Credential file: {cred_file.name}")
                    except Exception:
                        pass  # Ignore errors during purge

                # macOS Keychain items via `security` (current macOS backend).
                if self.platform == Platform.MACOS:
                    for username in usernames:
                        try:
                            macos_keychain.delete_password(SECURITY_SERVICE, username)
                            removed_items.append(f"Credential: {username}")
                        except Exception:
                            pass  # Ignore errors during purge
                        try:
                            macos_keychain.delete_password(
                                LEGACY_BACKUP_SECURITY_SERVICE, username
                            )
                            removed_items.append(
                                f"Legacy claude-swap credential: {username}"
                            )
                        except Exception:
                            pass

                # Best-effort sweep of any pre-migration keyring / Credential
                # Manager entries left behind by an incomplete keyring → files
                # (Windows) or keyring → security (macOS) migration. Linux/WSL
                # never used a keyring backend.
                if self.platform in (Platform.MACOS, Platform.WINDOWS):
                    _sweep_legacy_keyring(usernames, removed_items)

        # Session-profile keychain entries must go BEFORE the backup dir:
        # the hashed service names are derived from the dir paths and can't
        # be recomputed once the directories are deleted.
        if session_dirs:
            from openswap.session import delete_macos_keychain_entry

            for d in session_dirs:
                delete_macos_keychain_entry(d)
            removed_items.append(
                f"Session profiles: {', '.join(d.name for d in session_dirs)}"
            )

        # Remove managed data, but preserve settings, provider and lifecycle
        # lock files and their parent directories. Unlinking a held lock inode
        # would let another process create a new lock at the same path and
        # bypass it.
        if self.backup_dir.exists():
            # Close log handlers before deleting (required on Windows)
            for handler in self._logger.handlers[:]:
                handler.close()
                self._logger.removeHandler(handler)

            self._remove_backup_data_preserving_locks()
            removed_items.append(
                f"Backup data: {self.backup_dir} "
                "(settings, provider and lifecycle lock files retained)"
            )

        # Also clean a stale legacy directory if it somehow still exists
        # (e.g. a partial pre-migration state, or files re-created after init).
        if legacy_distinct and legacy.exists():
            try:
                shutil.rmtree(legacy)
                removed_items.append(f"Legacy directory: {legacy}")
            except OSError:
                pass

        if removed_items:
            print(f"\n{accent('Removed:')}")
            for item in removed_items:
                print(f"  {dimmed('-')} {item}")
        else:
            print(f"\n{dimmed('No openswap data found to remove.')}")

        print(f"\n{accent('Purge complete.')}")

    def _remove_backup_data_preserving_locks(self) -> None:
        """Remove backup data without unlinking held settings/provider locks."""
        root = self.backup_dir
        for child in list(root.iterdir()):
            if child.name == ".settings.lock":
                if child.is_symlink() or not child.is_file():
                    raise SessionError("Settings lock path is unsafe; refusing purge.")
                continue
            if child.name == ".lock":
                if child.is_symlink() or not child.is_file():
                    raise SessionError("Claude provider lock path is unsafe; refusing purge.")
                continue
            if child.name == "codex" and not child.is_symlink() and child.is_dir():
                for codex_child in list(child.iterdir()):
                    if codex_child.name == ".lock":
                        if codex_child.is_symlink() or not codex_child.is_file():
                            raise SessionError("Codex provider lock path is unsafe; refusing purge.")
                        continue
                    self._remove_purge_entry(codex_child)
                continue
            if child.name == "worker" and not child.is_symlink() and child.is_dir():
                for worker_child in list(child.iterdir()):
                    if worker_child.name == "lifecycle.lock":
                        if worker_child.is_symlink() or not worker_child.is_file():
                            raise SessionError(
                                "Worker lifecycle lock path is unsafe; refusing purge."
                            )
                        continue
                    if (
                        worker_child.name == "leases"
                        and not worker_child.is_symlink()
                        and worker_child.is_dir()
                    ):
                        # Keep the held unleased-run lock inodes (and their
                        # directory); remove every lease document.
                        for lease_child in list(worker_child.iterdir()):
                            if lease_child.name in _UNLEASED_LOCK_NAMES:
                                if lease_child.is_symlink() or not lease_child.is_file():
                                    raise SessionError(
                                        "Unleased-run lock path is unsafe; refusing purge."
                                    )
                                continue
                            self._remove_purge_entry(lease_child)
                        continue
                    self._remove_purge_entry(worker_child)
                continue
            self._remove_purge_entry(child)

    @staticmethod
    def _remove_purge_entry(path: Path) -> None:
        """Remove one entry without following a symlink to an external tree."""
        if path.is_symlink() or not path.is_dir():
            path.unlink()
        else:
            shutil.rmtree(path)


_UNLEASED_LOCK_NAMES = frozenset({"claude.unleased.lock", "codex.unleased.lock"})


def _path_is_within(path: Path, root: Path) -> bool:
    """Return whether ``path`` is ``root`` or one of its descendants."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
