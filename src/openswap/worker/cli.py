"""Early, credential-free CLI boundary for local worker operations."""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

from openswap import paths
from openswap.exceptions import ClaudeSwitchError, LockError
from openswap.locking import FileLock
from openswap.paths import get_backup_root
from openswap.settings import (
    AccountAllowlistFullError,
    MAX_ACCOUNT_ALLOWLIST,
    AllowlistedAccount,
    WorkerWorkspace,
    load_worker_settings,
    new_allowlist_ref,
    set_worker_pinned_account,
    set_worker_workspaces,
    update_worker_account_allowlist,
    update_worker_settings,
    valid_account_label,
)
from openswap.worker.accounts import (
    AccountChoices,
    AccountPinError,
    CodexAccountChoice,
    account_choices,
    resolve_account_selector,
    resolve_codex_selector,
)
from openswap.worker.client import WorkerClient
from openswap.worker.ipc import IpcError, socket_path
from openswap.worker.journal import MAX_EVENT_PAGE, LocalJobStore
from openswap.worker.launch_agent import install, uninstall
from openswap.worker.launch_agent import status as worker_service_status
from openswap.worker.leases import START_PENDING_REASON, AccountLeaseStore, ReleaseEvidence
from openswap.worker.models import JobState, SafeEventKind
from openswap.worker.runtime import (
    _pid_exists,
    output_dir_problem,
    read_worker_snapshot,
    readonly_source_problem,
)

_DISABLE_WAIT_SECONDS = 3.0
_DISABLE_POLL_SECONDS = 0.1
_DISABLE_EXIT_WAIT_SECONDS = 3.0
_LIFECYCLE_LOCK_TIMEOUT_SECONDS = 5.0
# Terminal states a lease release may follow. None of them proves on its own
# that the provider's process tree stopped (see _journal_proves_provider_stop).
# INTERRUPTED is terminal but records uncertainty, so it is not listed here.
# Journaled worker jobs have 32-hex ids; kickoff-* and usage-* probe leases do not.
_WORKER_JOB_ID = re.compile(r"[a-f0-9]{32}")
_ALLOWLIST_REF = re.compile(r"[0-9a-f]{32}")
_LEASE_TERMINAL_JOB_STATES = {
    JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.EXPIRED,
}


def _migrate_legacy_before_worker_state_change(backup_root: Path) -> None:
    """Run the canonical backup migration before worker code creates its root."""
    if paths.migrate_legacy_backup_dir(Path(backup_root)):
        print(
            f"openswap: migrated data from {paths.get_legacy_backup_root()} "
            f"to {backup_root}",
            file=sys.stderr,
        )


@contextmanager
def lifecycle_lock(backup_root: Path):
    """Serialize opt-in, pause and disable transitions across CLI/UI clients."""
    # FileLock creates its parent with the process umask (often 0755). Create
    # and validate the worker root first so lifecycle.lock remains under the
    # same 0700 boundary required by the journal and lease store.
    try:
        LocalJobStore(Path(backup_root))._ensure_private_dir()
    except Exception:
        raise ClaudeSwitchError("worker_state_unavailable") from None
    lock = FileLock(
        Path(backup_root) / "worker" / "lifecycle.lock",
        timeout=_LIFECYCLE_LOCK_TIMEOUT_SECONDS,
    )
    try:
        with lock:
            yield
    except Exception as exc:
        # Expose only a stable local diagnostic, never lock path/system detail.
        from openswap.exceptions import LockError

        if isinstance(exc, LockError):
            raise ClaudeSwitchError("worker_lifecycle_busy") from None
        raise


def _snapshot(backup_root: Path) -> dict:
    try:
        return WorkerClient(socket_path(backup_root)).status()
    except IpcError as exc:
        if str(exc) != "worker_unavailable":
            raise
        return read_worker_snapshot(backup_root).to_dict()


def read_status(backup_root: Path) -> dict:
    """Read worker status without constructing runtime or touching credentials."""
    return _snapshot(Path(backup_root))


def request_stop(backup_root: Path, job_id: str | None) -> dict:
    """Request interruption for one active job, optionally resolved by worker."""
    return WorkerClient(socket_path(Path(backup_root))).stop(job_id)


def request_pause(backup_root: Path, paused: bool) -> dict:
    """Persist admission policy and request the worker's barrier update."""
    root = Path(backup_root)
    _migrate_legacy_before_worker_state_change(root)
    with lifecycle_lock(root):
        previous = load_worker_settings(root)
        update_worker_settings(root, paused=paused)

        def restore_previous() -> bool:
            try:
                update_worker_settings(root, paused=previous.paused)
                return True
            except (OSError, RuntimeError, ValueError):
                return False

        try:
            result = WorkerClient(socket_path(root)).set_paused(paused)
        except IpcError as exc:
            if str(exc) != "worker_unavailable":
                restore_previous()
                raise
            result = {"accepted": True, "diagnostic_code": "worker_not_running"}
        if result.get("accepted") is not True:
            restored = restore_previous()
            if not restored:
                return {
                    "accepted": False,
                    "paused": load_worker_settings(root).paused,
                    "diagnostic_code": "settings_unavailable",
                }
        persisted = load_worker_settings(root)
        return {
            "accepted": result.get("accepted") is True,
            "paused": persisted.paused,
            "diagnostic_code": result.get("diagnostic_code"),
        }


def _managed_worker_loaded() -> bool:
    try:
        return worker_service_status().get("loaded") is True
    except (ClaudeSwitchError, OSError):
        return False


def enable_worker(backup_root: Path) -> dict:
    """Persist opt-in and idempotently install the per-user helper."""
    root = Path(backup_root)
    _migrate_legacy_before_worker_state_change(root)
    with lifecycle_lock(root):
        previous = load_worker_settings(root)
        if not _worker_instance_lock_is_free(root):
            # A held instance lock is fine only when it belongs to the already
            # loaded LaunchAgent (idempotent re-enable). Otherwise another
            # worker, such as a manual `openswap worker run`, owns it: a newly
            # bootstrapped service could never start, so refuse.
            if previous.enabled is not True:
                raise ClaudeSwitchError("worker_stop_unconfirmed")
            if not _managed_worker_loaded():
                raise ClaudeSwitchError("worker_running_unmanaged")
        # A scheduled kickoff running without a lease (Remote tasks off) holds
        # its provider's unleased-run lock; enabling now would let the worker
        # lease that account under it. Hold both locks across the opt-in.
        with ExitStack() as unleased:
            for provider in ("codex", "claude"):
                store = AccountLeaseStore(root, provider)
                if not unleased.enter_context(store.unleased_run(timeout=0)):
                    raise ClaudeSwitchError("kickoff_in_progress")
            persisted = update_worker_settings(root, enabled=True)
        if persisted.enabled is not True:
            # A malformed pinned account/workspace policy fails closed in the
            # settings parser. Never install a helper after that fail-closed
            # result, even if the write itself succeeded.
            try:
                update_worker_settings(root, enabled=previous.enabled)
            except (OSError, RuntimeError, ValueError):
                pass
            raise ClaudeSwitchError("worker_configuration_invalid")
        try:
            service = install()
        except ClaudeSwitchError:
            try:
                update_worker_settings(root, enabled=previous.enabled)
            except (OSError, RuntimeError, ValueError):
                pass
            raise
        return {"enabled": True, "service": service}


def worker_account_choices(backup_root: Path) -> AccountChoices:
    """Roster metadata and the allowlist for the account picker; reads no credentials."""
    root = Path(backup_root)
    policy = load_worker_settings(root)
    return account_choices(root, policy.pinned_account_ref, policy.account_allowlist)


def set_worker_account(backup_root: Path, selector: str | None):
    """Pin the account Remote tasks uses (``None`` clears the pin).

    The one function behind ``openswap worker account`` and the menu bar
    picker. It holds the lifecycle lock like every other worker settings
    change, and resolves the slot and writes the pin under both providers'
    mutation guards, so the slot cannot be removed, swapped or moved in
    between. Eligible: a Codex slot with a ChatGPT account ID, or a Claude
    slot (``claude:<slot>`` names one explicitly; a bare selector is a Codex
    slot first). The pin is typed (``codex:``/``claude:``), and the job runs
    on that provider. A running job is unaffected: it keeps the account
    recorded on it at STARTING, and the worker reads the new pin for the
    next launch.
    """
    root = Path(backup_root)

    def change():
        choice = None if selector is None else resolve_account_selector(root, selector)
        # A newly pinned account joins the allowlist (default label); clearing
        # the pin keeps the allowlist, so the account stays a per-job choice.
        set_worker_pinned_account(root, None if choice is None else choice.account_ref)
        return choice

    return _account_policy_change(root, change)


def _account_policy_change(root: Path, change):
    """Run one pin/allowlist change under the same locks as the pin.

    The worker lifecycle lock, then the Codex mutation guard, so a roster
    slot cannot be removed, swapped or moved while it is resolved and saved.
    Failures surface as ``AccountPinError`` with a stable code.
    """
    _migrate_legacy_before_worker_state_change(root)
    with lifecycle_lock(root):
        try:
            # Codex then Claude, the order every multi-provider holder uses.
            with AccountLeaseStore(root, "codex").mutation_guard(
                timeout=_LIFECYCLE_LOCK_TIMEOUT_SECONDS,
            ), AccountLeaseStore(root, "claude").mutation_guard(
                timeout=_LIFECYCLE_LOCK_TIMEOUT_SECONDS,
            ):
                return change()
        except AccountPinError:
            raise
        except LockError:
            raise AccountPinError("account_roster_busy") from None
        except AccountAllowlistFullError:
            raise AccountPinError("too_many_accounts") from None
        except (OSError, RuntimeError, ValueError):
            raise AccountPinError("settings_unavailable") from None


def _clean_label(label: str | None) -> str | None:
    if label is None:
        return None
    label = label.strip() if isinstance(label, str) else label
    if not valid_account_label(label):
        raise AccountPinError("label_invalid")
    return label


def _find_allowlisted(root: Path, entries, target: str) -> AllowlistedAccount:
    """An allowlist entry by its reference, its ``codex:`` identity, or a roster selector.

    The reference and identity forms also reach an entry whose account has
    left the Codex roster, so it can still be disallowed.
    """
    target = target.strip() if isinstance(target, str) else ""
    for entry in entries:
        if target in {entry.account_ref, entry.identity}:
            return entry
    if not target or _ALLOWLIST_REF.fullmatch(target):
        raise AccountPinError("account_not_allowlisted")
    identity = resolve_account_selector(root, target).account_ref
    match = next((entry for entry in entries if entry.identity == identity), None)
    if match is None:
        raise AccountPinError("account_not_allowlisted")
    return match


def allow_worker_account(backup_root: Path, selector: str, label: str | None = None) -> AllowlistedAccount:
    """Allowlist a Codex account so a control service may choose it per job.

    The account gets a fresh random reference (never derived from it) the
    first time; allowing it again keeps that reference and only changes the
    label when one is given. Codex slots with a ChatGPT account ID and Claude
    slots; at most 20 accounts.
    """
    root = Path(backup_root)
    label = _clean_label(label)

    def change():
        choice = resolve_account_selector(root, selector)
        result = {}

        def update(pin, entries):
            entries = list(entries)
            index = next((i for i, e in enumerate(entries) if e.identity == choice.account_ref), None)
            if index is None:
                if len(entries) >= MAX_ACCOUNT_ALLOWLIST:
                    raise AccountPinError("too_many_accounts")
                from openswap.worker.accounts import default_account_label

                entries.append(AllowlistedAccount(
                    new_allowlist_ref(), choice.account_ref,
                    label or default_account_label(root, choice.account_ref),
                ))
                index = len(entries) - 1
            elif label is not None:
                entries[index] = AllowlistedAccount(entries[index].account_ref, entries[index].identity, label)
            result["entry"] = entries[index]
            return pin, entries

        update_worker_account_allowlist(root, update)
        return result["entry"]

    return _account_policy_change(root, change)


def disallow_worker_account(
    backup_root: Path, target: str, *, clear_default: bool = False,
) -> AllowlistedAccount:
    """Withdraw an account from the allowlist (by selector, reference or identity).

    The default (pinned) account is refused unless ``clear_default``, which
    also clears the pin. A job already recorded with this account's reference
    then fails before launch; the worker never substitutes another account.
    """
    root = Path(backup_root)

    def change():
        result = {}

        def update(pin, entries):
            entry = _find_allowlisted(root, entries, target)
            if entry.identity == pin:
                if not clear_default:
                    raise AccountPinError("account_is_default")
                pin = None
            result["entry"] = entry
            return pin, [e for e in entries if e.account_ref != entry.account_ref]

        update_worker_account_allowlist(root, update)
        return result["entry"]

    return _account_policy_change(root, change)


def label_worker_account(backup_root: Path, target: str, label: str) -> AllowlistedAccount:
    """Rename an allowlisted account's owner-chosen label; its reference is kept."""
    root = Path(backup_root)
    label = _clean_label(label)

    def change():
        result = {}

        def update(pin, entries):
            entry = _find_allowlisted(root, entries, target)
            renamed = AllowlistedAccount(entry.account_ref, entry.identity, label)
            result["entry"] = renamed
            return pin, [renamed if e.account_ref == entry.account_ref else e for e in entries]

        update_worker_account_allowlist(root, update)
        return result["entry"]

    return _account_policy_change(root, change)


class WorkspaceError(ClaudeSwitchError):
    """A workspace change was refused; ``code`` is a stable reason."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _protected_dirs(root: Path) -> list[Path]:
    from openswap.codex.auth import codex_home
    from openswap.paths import get_claude_config_home

    out = []
    for path in (root, codex_home(), get_claude_config_home()):
        try:
            out.append(Path(path).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
    return out


def _overlaps_credentials(root: Path, folder: Path, *, writable: bool) -> bool:
    """Whether a folder would expose OpenSwap or provider credential homes.

    No approved folder may be the home directory, or contain the backup root,
    Codex home or Claude config home. A read-only source may not sit inside
    one either. A writable root may sit inside the backup root's ``worker``
    directory only (where the default ``research`` workspace lives).
    """
    protected = _protected_dirs(root)
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None
    for path in [*protected, *([home] if home else [])]:
        if path == folder or path.is_relative_to(folder):
            return True
    try:
        backup = Path(root).expanduser().resolve()
    except (OSError, RuntimeError):
        backup = None
    for path in protected:
        if folder.is_relative_to(path):
            # Only the backup root itself has the `worker` exception: a Codex or
            # Claude home configured beneath that directory stays off limits.
            if writable and backup is not None and path == backup and folder.is_relative_to(backup / "worker"):
                continue
            return True
    return False


def _absolute_folder(folder: str | Path) -> Path:
    text = str(folder)
    if not text or len(text) > 2048 or any(ord(c) < 32 for c in text):
        raise WorkspaceError("folder_invalid")
    try:
        return Path(text).expanduser().absolute()
    except (OSError, RuntimeError):
        raise WorkspaceError("folder_invalid") from None


def add_worker_workspace(
    backup_root: Path,
    workspace_id: str,
    folder: str | Path,
    readonly_sources: tuple[str | Path, ...] = (),
) -> WorkerWorkspace:
    """Approve ``folder`` as the writable research root for ``workspace_id``.

    The ID is the opaque name a remote submission uses (OpenTag's "Local
    workspace IDs"); the path never leaves this Mac. A missing folder is
    created owner-only (0700). The folder must pass the same checks the
    worker applies at launch: a real directory owned by this user with no
    group or other access. Read-only sources must exist, be owned by this
    user and not be writable by others.
    """
    from openswap.settings import _WORKSPACE_ID_RE

    root = Path(backup_root)
    if not isinstance(workspace_id, str) or not _WORKSPACE_ID_RE.fullmatch(workspace_id):
        raise WorkspaceError("workspace_id_invalid")
    output = _absolute_folder(folder)
    sources = tuple(_absolute_folder(item) for item in readonly_sources)
    if len(sources) > 16:
        raise WorkspaceError("too_many_readonly_sources")
    _migrate_legacy_before_worker_state_change(root)
    with lifecycle_lock(root):
        current = load_worker_settings(root)
        if any(item.workspace_id == workspace_id for item in current.workspaces):
            raise WorkspaceError("workspace_exists")
        if len(current.workspaces) >= 16:
            raise WorkspaceError("too_many_workspaces")
        try:
            output.mkdir(mode=0o700, parents=True, exist_ok=True)
            output = output.resolve()
            sources = tuple(source.resolve() for source in sources)
        except (OSError, RuntimeError):
            raise WorkspaceError("folder_unavailable") from None
        if _overlaps_credentials(root, output, writable=True):
            raise WorkspaceError("folder_exposes_credentials")
        problem = output_dir_problem(output)
        if problem is not None:
            raise WorkspaceError(f"folder_{problem}")
        for source in sources:
            if _overlaps_credentials(root, source, writable=False):
                raise WorkspaceError("readonly_source_exposes_credentials")
            problem = readonly_source_problem(source)
            if problem is not None:
                raise WorkspaceError(f"readonly_source_{problem}")
        workspace = WorkerWorkspace(workspace_id, output, sources)
        try:
            set_worker_workspaces(root, (*current.workspaces, workspace))
        except ValueError as exc:
            if "disjoint" in str(exc):
                raise WorkspaceError("readonly_source_overlaps_folder") from None
            raise WorkspaceError("settings_unavailable") from None
        except (OSError, RuntimeError):
            raise WorkspaceError("settings_unavailable") from None
        return workspace


def _unsynced_remote_work(root: Path) -> tuple[tuple[str, ...], frozenset[str]]:
    """Remote claims whose results are not yet synchronized, at any service.

    Returns the local job IDs of admitted ones and the workspace IDs named by
    claims remembered but not yet admitted (a restart admits them later).
    """
    path = LocalJobStore(root).state_dir / "remote.sqlite3"
    if not path.exists():
        return (), frozenset()
    if path.is_symlink():
        raise ValueError("unsafe remote journal")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bindings'").fetchone()
        if exists is None:
            return (), frozenset()
        rows = db.execute("SELECT local_id, claim FROM bindings WHERE done=0").fetchall()
    finally:
        db.close()
    local_ids, workspaces = [], set()
    for local_id, claim in rows:
        if local_id is not None:
            local_ids.append(local_id)
            continue
        try:
            workspace = json.loads(claim)["submission"]["workspace_id"]
        except (ValueError, TypeError, KeyError):
            raise ValueError("unreadable remote claim") from None
        if not isinstance(workspace, str):
            raise ValueError("unreadable remote claim")
        workspaces.add(workspace)
    return tuple(local_ids), frozenset(workspaces)


def remove_worker_workspace(backup_root: Path, workspace_id: str) -> None:
    """Withdraw approval for a workspace ID; its folder and files are kept.

    Removing the last one is refused: an empty registry makes the worker's
    policy fail closed. A job already running keeps its resolved folder.
    """
    root = Path(backup_root)
    _migrate_legacy_before_worker_state_change(root)
    with lifecycle_lock(root):
        current = load_worker_settings(root)
        remaining = tuple(item for item in current.workspaces if item.workspace_id != workspace_id)
        if len(remaining) == len(current.workspaces):
            raise WorkspaceError("workspace_not_found")
        if not remaining:
            raise WorkspaceError("last_workspace")
        # Artifact upload re-reads the registry for the job's folder, so a
        # mapping stays while any job using it may still run or upload. (A job
        # that resolves the folder in the moment before this write still runs
        # there; its upload then needs the ID added back to the same folder.)
        try:
            unsynced, unadmitted = _unsynced_remote_work(root)
            in_use = workspace_id in unadmitted or LocalJobStore(root).workspace_in_use(workspace_id, unsynced)
        except (OSError, sqlite3.Error, ValueError):
            raise WorkspaceError("settings_unavailable") from None
        if in_use:
            raise WorkspaceError("workspace_in_use")
        try:
            set_worker_workspaces(root, remaining)
        except (OSError, RuntimeError, ValueError):
            raise WorkspaceError("settings_unavailable") from None


def _write(payload: dict, *, as_json: bool, human: str | None = None) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(human if human is not None else json.dumps(payload, sort_keys=True))


def _safe_to_disable(snapshot: dict) -> str | None:
    """Return a safe diagnostic if work or its account lease may remain live."""
    if snapshot.get("lease_quarantined") is not False:
        return "lease_state_unknown"
    if snapshot.get("process_state") in {"stale", "unavailable", "starting", "stopping"}:
        return "worker_state_unknown"
    if snapshot.get("active_job") is not None:
        return "job_still_active"
    if snapshot.get("process_state") not in {"stopped", "running"}:
        return "worker_state_unknown"
    return None


def disable_worker(backup_root: Path) -> tuple[bool, dict, str | None]:
    """Pause first; only unload after the worker proves idle and unleased."""
    _migrate_legacy_before_worker_state_change(backup_root)
    with lifecycle_lock(backup_root):
        return _disable_locked(backup_root)


def _worker_instance_lock_is_free(backup_root: Path) -> bool:
    """Acquire/release the process-lifetime lock as a shutdown proof."""
    path = Path(backup_root) / "worker" / "instance.lock"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        return False
    lock = FileLock(path, timeout=0)
    try:
        if not lock.acquire(timeout=0):
            return False
    except Exception:
        return False
    try:
        lock.release()
    except Exception:
        return False
    return True


def _wait_for_worker_exit(backup_root: Path) -> bool:
    """Wait boundedly for the daemon to release its process-lifetime lock."""
    deadline = time.monotonic() + _DISABLE_EXIT_WAIT_SECONDS
    while True:
        if _worker_instance_lock_is_free(backup_root):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_DISABLE_POLL_SECONDS, remaining))


def _safe_snapshot(backup_root: Path) -> dict:
    """Best-effort fail-closed snapshot for control error paths."""
    try:
        return _snapshot(backup_root)
    except Exception:
        try:
            return read_worker_snapshot(backup_root).to_dict()
        except Exception:
            return {
                "enabled": True,
                "paused": True,
                "process_state": "unavailable",
                "active_job": None,
                "lease_quarantined": True,
            }


def _disable_locked(backup_root: Path) -> tuple[bool, dict, str | None]:
    # Pause without touching the opt-in: retrying disable on an already
    # disabled worker must never re-enable it, even when a check blocks.
    try:
        was_enabled = update_worker_settings(backup_root, paused=True).enabled
    except (OSError, RuntimeError, ValueError):
        return False, {}, "settings_unavailable"
    client = WorkerClient(socket_path(backup_root))
    try:
        pause_result = client.set_paused(True)
        if pause_result.get("accepted") is not True:
            return _blocked(
                backup_root, was_enabled, _safe_snapshot(backup_root),
                pause_result.get("diagnostic_code") or "pause_refused",
            )
    except IpcError as exc:
        if str(exc) != "worker_unavailable":
            return _blocked(backup_root, was_enabled, _safe_snapshot(backup_root), str(exc))

    try:
        snapshot = _snapshot(backup_root)
    except IpcError as exc:
        return _blocked(backup_root, was_enabled, _safe_snapshot(backup_root), str(exc))
    except Exception:
        return _blocked(
            backup_root, was_enabled, _safe_snapshot(backup_root), "worker_status_unavailable"
        )
    active = snapshot.get("active_job")
    if active is not None:
        job_id = active.get("job_id") if isinstance(active, dict) else None
        if not isinstance(job_id, str) or not job_id:
            return _blocked(backup_root, was_enabled, snapshot, "job_identity_unknown")
        try:
            result = client.stop(job_id)
        except IpcError as exc:
            return _blocked(backup_root, was_enabled, _safe_snapshot(backup_root), str(exc))
        except Exception:
            return _blocked(
                backup_root, was_enabled, _safe_snapshot(backup_root), "worker_status_unavailable"
            )
        if result.get("accepted") is not True:
            return _blocked(
                backup_root,
                was_enabled,
                _safe_snapshot(backup_root),
                result.get("diagnostic_code") or "stop_refused",
            )

        deadline = time.monotonic() + _DISABLE_WAIT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(_DISABLE_POLL_SECONDS)
            try:
                snapshot = _snapshot(backup_root)
            except IpcError as exc:
                return _blocked(
                    backup_root,
                    was_enabled,
                    _safe_snapshot(backup_root),
                    str(exc),
                )
            except Exception:
                return _blocked(
                    backup_root,
                    was_enabled,
                    _safe_snapshot(backup_root),
                    "worker_status_unavailable",
                )
            if _safe_to_disable(snapshot) is None:
                break
        else:
            return _blocked(
                backup_root,
                was_enabled,
                snapshot,
                _safe_to_disable(snapshot) or "job_stop_not_confirmed",
            )

    diagnostic = _safe_to_disable(snapshot)
    if diagnostic is not None:
        return _blocked(backup_root, was_enabled, snapshot, diagnostic)

    # Policy is changed only after the active job and lease are confirmed safe.
    try:
        update_worker_settings(backup_root, enabled=False, paused=True)
    except (OSError, RuntimeError, ValueError):
        return _blocked(backup_root, was_enabled, snapshot, "settings_unavailable")
    try:
        service = uninstall(home=Path.home())
    except ClaudeSwitchError:
        # The unload failed, so the committed opt-out is rolled back to the
        # prior opt-in (never forced on); pause stays on so a still-loaded
        # helper cannot admit work.
        try:
            update_worker_settings(backup_root, enabled=was_enabled, paused=True)
        except (OSError, RuntimeError, ValueError):
            return False, _safe_snapshot(backup_root), "settings_unavailable"
        return False, _safe_snapshot(backup_root), "worker_unload_failed"
    if not _wait_for_worker_exit(backup_root):
        # Opt-out stays committed. Re-enabling policy here could let the
        # lingering manual worker continue admitting work before it exits.
        return False, _safe_snapshot(backup_root), "worker_stop_unconfirmed"
    return True, {"snapshot": _safe_snapshot(backup_root), "service": service}, None


def _blocked(
    backup_root: Path, was_enabled: bool, snapshot: dict, diagnostic: str
) -> tuple[bool, dict, str]:
    """Keep the prior opt-in, paused, whenever safe disable is unproved."""
    try:
        update_worker_settings(backup_root, enabled=was_enabled, paused=True)
    except (OSError, RuntimeError, ValueError):
        diagnostic = "settings_unavailable"
    return False, snapshot, diagnostic


def release_lease(
    backup_root: Path, provider: str = "codex", *, confirm_stopped: bool = False
) -> tuple[bool, dict, str | None]:
    """Manually release a stuck ``active``/``uncertain`` account lease.

    This is the only supported way to clear a lease whose holder never
    resolved it. It never auto-releases and never relaunches anything; it
    releases only after this proves the holder is gone:

    - a worker job lease needs its recording worker gone (a newer epoch has
      started, or its pid is no longer alive), its job terminal, and the
      provider's own proof that its process tree stopped: a journaled
      ``provider_finished`` event with ``execution_stopped``. A terminal state
      alone is not that proof (the worker can die between the provider
      finishing and the lease release), so without the event it also needs
      ``confirm_stopped``;
    - a worker lease whose job is ``interrupted`` or missing from the journal
      also needs ``confirm_stopped``: neither is stop evidence;
    - while the recording worker is still running, its lease can be released
      only with ``confirm_stopped`` and only once the journal shows the job
      terminal: the worker has then dropped the lease and admits no new work
      on the quarantined account, so the owner's confirmation is the same
      evidence as after the worker exited. A lease still marked
      ``start_pending`` (an abandoned ``start()`` that has not returned) is
      refused until the worker records that the call returned;
    - a short-lived probe lease (scheduled kickoff, Codex usage read: any
      lease whose job is not a journaled worker job) is never journaled, and a
      provider helper can outlive both a timeout and a killed menu process, so
      it needs the lease expired and ``confirm_stopped`` (the owner attests
      that no such process is still running), plus its recording pid gone
      while the lease is still ``active`` (an ``uncertain`` one was already
      given up by its long-lived holder).

    Otherwise it refuses. The release is recorded as ``owner_released``.
    """
    if provider not in {"codex", "claude"}:
        raise ValueError("unsupported lease provider")
    root = Path(backup_root)
    _migrate_legacy_before_worker_state_change(root)
    job_store = LocalJobStore(root)
    lease_store = AccountLeaseStore(root, provider)
    with lifecycle_lock(root):
        with lease_store.mutation_guard() as guard:
            lease = guard.current()
            if lease is None:
                return False, {}, "lease_not_found"
            if lease.state == "released":
                return True, {"lease_state": "released"}, "already_released"
            if lease.state not in {"active", "uncertain"}:
                return False, {}, "lease_state_unknown"
            pid_gone = not _pid_exists(lease.worker_pid)
            if not _WORKER_JOB_ID.fullmatch(lease.job_id):
                if lease.state == "active" and not pid_gone:
                    return False, {}, "worker_owner_may_be_alive"
                if lease.expires_at > lease_store._now():
                    return False, {}, "lease_not_expired"
                if not confirm_stopped:
                    return False, {}, "stop_unproven_confirm_required"
            else:
                owner_gone = job_store.current_epoch() > lease.worker_epoch or pid_gone
                try:
                    job_state = job_store.get(lease.job_id).state
                except KeyError:
                    job_state = None
                if not owner_gone:
                    job_terminal = job_state == JobState.INTERRUPTED or (
                        job_state in _LEASE_TERMINAL_JOB_STATES
                    )
                    if not job_terminal:
                        return False, {}, "worker_owner_may_be_alive"
                    if lease.reason == START_PENDING_REASON:
                        # An abandoned start() may still launch: wait for the
                        # worker to record that it returned.
                        return False, {}, "provider_start_pending"
                    if not confirm_stopped:
                        return False, {}, "stop_unproven_confirm_required"
                elif job_state is None or job_state == JobState.INTERRUPTED:
                    if not confirm_stopped:
                        return False, {}, "stop_unproven_confirm_required"
                elif job_state not in _LEASE_TERMINAL_JOB_STATES:
                    return False, {}, "job_not_terminal"
                elif not confirm_stopped and not _journal_proves_provider_stop(
                    job_store, lease.job_id
                ):
                    return False, {}, "stop_unproven_confirm_required"
            guard.release(lease.token(), ReleaseEvidence.OWNER_RELEASED)
            return True, {"lease_state": "released"}, None


def _journal_proves_provider_stop(job_store: LocalJobStore, job_id: str) -> bool:
    """Whether the journal holds the provider's proof that its process tree stopped.

    The runtime accepts a ``provider_finished`` event with ``execution_stopped``
    only from the adapter's own report that the run and its descendants have
    ended, and journals it before the job turns terminal. Every other path to
    a terminal state either releases the lease itself first or quarantines
    it, so a lease left behind without this event has no stop evidence.
    """
    after = 0
    while True:
        page = job_store.list_events(job_id, after_cursor=after, limit=MAX_EVENT_PAGE)
        if any(
            event.kind == SafeEventKind.PROVIDER_FINISHED and event.execution_stopped is True
            for event in page.events
        ):
            return True
        if not page.events:
            return False
        after = page.next_cursor


def _run(backup_root: Path, *, managed: bool = False) -> int:
    try:
        with lifecycle_lock(backup_root):
            if not load_worker_settings(backup_root).enabled:
                print("Worker is disabled. Enable Remote tasks before starting it.")
                return 0
    except ClaudeSwitchError:
        print("Worker lifecycle is busy; try again shortly.", file=sys.stderr)
        return 1
    # Imported only in the dedicated worker command, after the default-off
    # policy check. The runtime owns its own SQLite journal and local socket.
    from openswap.worker.runtime import WORKER_REFUSED_MANAGED, run_worker

    result = int(run_worker(backup_root, managed=managed, service_loaded=_managed_worker_loaded))
    if result == WORKER_REFUSED_MANAGED:
        print(
            "The Remote tasks LaunchAgent is running the worker; "
            "disable it before running the worker manually.",
            file=sys.stderr,
        )
        return 1
    return result


def main(argv: list[str] | None = None, *, backup_root: Path | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "refserver":
        from openswap.worker.refserver.cli import main as refserver_main
        return refserver_main(arguments[1:])
    if arguments[:1] == ["live-check"]:
        from openswap.worker.live_check import main as live_check_main
        root = Path(backup_root) if backup_root is not None else get_backup_root()
        return live_check_main(arguments, root, migrate=_migrate_legacy_before_worker_state_change)
    if arguments[:1] in (["codex"], ["live"], ["claude"]):
        from openswap.worker.live_cli import main as live_main
        root = Path(backup_root) if backup_root is not None else get_backup_root()
        return live_main(arguments, root, migrate=_migrate_legacy_before_worker_state_change)
    if arguments[:1] == ["account"] and len(arguments) > 1 and arguments[1] in _ALLOWLIST_COMMANDS:
        root = Path(backup_root) if backup_root is not None else get_backup_root()
        return _allowlist_command(root, arguments[1:])
    parser = argparse.ArgumentParser(
        prog="openswap worker",
        description="Control the opt-in Remote Agent Host worker.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("refserver", help="serve the reference protocol or manage pairing/revocation")
    commands.add_parser("codex", help="install, verify and sign in the pinned Codex CLI that remote jobs run")
    commands.add_parser("live", help="show or change the explicit live-execution opt-in")
    commands.add_parser("claude", help="pin the Claude Code CLI and prepare account profiles for Remote tasks")
    commands.add_parser("live-check", help="run real Codex or Claude jobs on this Mac and record phase-1 live evidence")
    run = commands.add_parser("run", help="run the background worker process")
    # Passed only by the LaunchAgent: a manual run refuses while it is loaded.
    run.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)
    pair_parser = commands.add_parser("pair", help="approve enrollment locally and store its device key in login Keychain")
    pair_parser.add_argument("url")
    pair_parser.add_argument("code")
    unpair_parser = commands.add_parser(
        "unpair", help="remove the device key and configured service URL; pass a URL to remove an enrollment "
                       "that settings no longer reference",
    )
    unpair_parser.add_argument("url", nargs="?", default=None, help="service URL to unpair (default: the configured one)")
    submit_parser = commands.add_parser("submit-test", help="TEST ONLY: submit a bounded research job to a paired service")
    submit_parser.add_argument("--url", required=True)
    submit_parser.add_argument("--task", required=True)
    submit_parser.add_argument("--workspace-id", required=True)
    submit_parser.add_argument("--runtime-limit", required=True, type=float)
    expiry = submit_parser.add_mutually_exclusive_group(required=True)
    expiry.add_argument("--expires-in", type=float, help="seconds from now (at most 86400)")
    expiry.add_argument("--expires-at", help="absolute RFC 3339 expiry; a retry must repeat the one it printed")
    submit_parser.add_argument("--idempotency-key", default=None,
                               help="reuse the key printed by a failed attempt so a retry cannot admit a second job")
    submit_parser.add_argument("--account-ref", default=None,
                               help="optional: an account reference the worker advertised (account choice extension)")
    submit_parser.add_argument("--i-understand-this-is-a-test-tool", action="store_true")
    status_parser = commands.add_parser("status", help="show local worker status")
    status_parser.add_argument("--json", action="store_true")
    stop_parser = commands.add_parser("stop", help="request interruption of the active job")
    stop_parser.add_argument("job_id", nargs="?", help="omit to target the active job")
    stop_parser.add_argument("--json", action="store_true")
    pause_parser = commands.add_parser("pause", help="pause or reopen job admission")
    pause_parser.add_argument("--off", action="store_true", help="reopen admission")
    pause_parser.add_argument("--json", action="store_true")
    enable_parser = commands.add_parser("enable", help="opt in and install the worker LaunchAgent")
    enable_parser.add_argument("--json", action="store_true")
    disable_parser = commands.add_parser("disable", help="safely stop and disable the worker")
    disable_parser.add_argument("--json", action="store_true")
    lease_parser = commands.add_parser("lease", help="manage the local worker's account lease")
    lease_commands = lease_parser.add_subparsers(dest="lease_command", required=True)
    lease_release_parser = lease_commands.add_parser(
        "release", help="release a stuck lease once its worker is proven gone"
    )
    lease_release_parser.add_argument("--provider", choices=("codex", "claude"), default="codex")
    lease_release_parser.add_argument(
        "--confirm-stopped", action="store_true",
        help="attest that no process using the account is still running, when that cannot be proven",
    )
    lease_release_parser.add_argument("--json", action="store_true")
    account_parser = commands.add_parser(
        "account",
        help="list or pin the Codex or Claude account remote jobs run on",
        description="With no argument, list Codex and Claude roster slots, the accounts allowed for a "
                    "per-job choice, and mark the pinned default. Pass a slot, email or alias to "
                    "pin that account for the next job (`claude:4` or `codex:2` names the provider; "
                    "a bare slot means Codex first). `account allow|disallow|label` manage "
                    "the accounts a control service may choose per job (see `account allow --help`).",
    )
    account_parser.add_argument("selector", nargs="?", metavar="SLOT|EMAIL|ALIAS")
    account_parser.add_argument("--clear", action="store_true", help="remove the pin")
    account_parser.add_argument("--json", action="store_true")
    workspace_parser = commands.add_parser(
        "workspace", help="manage the approved research folders and their opaque IDs",
    )
    workspace_commands = workspace_parser.add_subparsers(dest="workspace_command", required=True)
    workspace_list = workspace_commands.add_parser("list", help="list approved research folders")
    workspace_list.add_argument("--json", action="store_true")
    workspace_add = workspace_commands.add_parser(
        "add", help="approve a research folder under an ID the control service will send",
    )
    workspace_add.add_argument("workspace_id", metavar="ID")
    workspace_add.add_argument("folder", metavar="FOLDER")
    workspace_add.add_argument(
        "--readonly-source", action="append", metavar="DIR",
        help="a source checkout the job may read but never write (repeatable)",
    )
    workspace_add.add_argument("--json", action="store_true")
    workspace_remove = workspace_commands.add_parser("remove", help="withdraw approval for a workspace ID")
    workspace_remove.add_argument("workspace_id", metavar="ID")
    workspace_remove.add_argument("--json", action="store_true")
    args = parser.parse_args(arguments)
    root = Path(backup_root) if backup_root is not None else get_backup_root()

    if args.command == "submit-test":
        import shlex
        from uuid import uuid4
        from openswap.worker.submit_test import resolve_expiry, submit_test
        from openswap.worker.protocol import ProtocolError, stamp
        key = uuid4().hex if args.idempotency_key is None else args.idempotency_key
        expires_at = None
        try:
            # A retry (explicit key and expiry) resends the original payload even
            # after its expiry, so the service can return the original job.
            expires_at = resolve_expiry(expires_in=args.expires_in, expires_at=args.expires_at,
                                        allow_past=args.idempotency_key is not None and args.expires_at is not None)
            result = submit_test(root, url=args.url, task=args.task, workspace_id=args.workspace_id,
                                 runtime_limit=args.runtime_limit, expires_at=expires_at,
                                 acknowledged=args.i_understand_this_is_a_test_tool, idempotency_key=key,
                                 account_ref=args.account_ref)
        except ProtocolError as exc:
            print(f"Test submission refused: {exc.code}.", file=sys.stderr)
        except (OSError, RuntimeError, ValueError):
            print("Test submission refused: local configuration unavailable.", file=sys.stderr)
        else:
            print(json.dumps(result))
            return 0
        # The service may have committed the job before the response was lost:
        # a retry must resend the identical submission (same key, same absolute
        # expiry) so it returns that job instead of admitting a second one.
        # The attached --flag=value form keeps a value that starts with "-" an argument.
        retry = f"--idempotency-key={shlex.quote(key)}"
        if expires_at is not None:
            retry += f" --expires-at={shlex.quote(stamp(expires_at))}"
        if args.account_ref is not None:
            retry += f" --account-ref={shlex.quote(args.account_ref)}"
        print(f"Retry with {retry} to reuse the same submission.", file=sys.stderr)
        return 1

    if args.command in {"pair", "unpair"}:
        from openswap.worker.pairing import pair, unpair
        from openswap.worker.protocol import ProtocolError
        try:
            # Both write under the backup root (settings, the pairing lock), so legacy
            # data must move first or a later enable finds both roots populated.
            _migrate_legacy_before_worker_state_change(root)
        except ClaudeSwitchError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        try:
            if args.command == "pair":
                worker_id = pair(root, args.url, args.code)
                print(f"Paired worker {worker_id}. Local execution policy is still controlled on this Mac.")
                interactive = _interactive_terminal()
                try:
                    _post_pair_setup(root, interactive=interactive)
                except Exception:
                    # Pairing already succeeded; the follow-up is optional.
                    print("Next: `openswap worker account` and `openswap worker workspace add <id> <folder>`.")
                try:
                    _post_pair_worker_offer(root, interactive=interactive)
                except Exception:
                    # Same rule: the offer can never fail pairing.
                    print(_START_WORKER_NEXT)
            else:
                if unpair(root, args.url):
                    print("Worker unpaired; remote access disabled.")
                elif load_worker_settings(root).control_service_url is not None:
                    # Only an orphan was removed: the configured enrollment is still live.
                    print("Removed the saved enrollment for that service. Remote access to the "
                          "configured service is unchanged; run `unpair` without a URL to disable it.")
                else:
                    print("Removed any saved enrollment for that service; no control service is configured.")
            return 0
        except ProtocolError as exc:
            print(f"Could not {args.command}: {exc.code}.", file=sys.stderr)
            return 1
        except (OSError, RuntimeError, ValueError):
            print(f"Could not {args.command}: local settings unavailable.", file=sys.stderr)
            return 1

    if args.command in {"run", "enable", "disable", "pause"}:
        try:
            _migrate_legacy_before_worker_state_change(root)
        except ClaudeSwitchError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    if args.command == "run":
        return _run(root, managed=args.managed)
    if args.command == "account":
        return _account_command(root, args)
    if args.command == "workspace":
        return _workspace_command(root, args)
    if args.command == "status":
        try:
            snapshot = read_status(root)
        except Exception:
            print("Worker status unavailable.", file=sys.stderr)
            return 1
        human = None
        if not args.json:
            human = _format_status(snapshot)
            hint = _worker_off_hint(root, snapshot)
            if hint is not None:
                human = f"{human}\n{hint}"
        _write(snapshot, as_json=args.json, human=human)
        return 0
    if args.command == "stop":
        try:
            result = request_stop(root, args.job_id)
        except IpcError as exc:
            print(f"Could not request worker stop: {exc}", file=sys.stderr)
            return 1
        if result.get("accepted") is not True:
            _write(result, as_json=args.json, human="Worker refused the stop request.")
            return 1
        _write(
            result,
            as_json=args.json,
            human="Stop requested; execution status will update when confirmed.",
        )
        return 0
    if args.command == "pause":
        paused = not args.off
        try:
            payload = request_pause(root, paused)
        except (IpcError, ClaudeSwitchError):
            print("Could not update worker admission.", file=sys.stderr)
            return 1
        except (OSError, RuntimeError, ValueError):
            print("Could not update worker admission settings.", file=sys.stderr)
            return 1
        _write(
            payload,
            as_json=args.json,
            human="Worker admission paused." if paused else "Worker admission reopened.",
        )
        return 0 if payload["accepted"] else 1
    if args.command == "enable":
        try:
            payload = enable_worker(root)
        except ClaudeSwitchError as exc:
            print(_enable_failure_message(exc), file=sys.stderr)
            return 1
        _write(payload, as_json=args.json, human="Remote tasks worker enabled.")
        return 0
    if args.command == "disable":
        try:
            ok, result, diagnostic = disable_worker(root)
        except ClaudeSwitchError:
            print("Worker lifecycle is busy; try again shortly.", file=sys.stderr)
            return 1
        if not ok:
            if diagnostic == "worker_stop_unconfirmed":
                print(
                    "Worker stop is not confirmed; it remains disabled and "
                    "admission-paused. Wait for it to exit before enabling.",
                    file=sys.stderr,
                )
            else:
                # A blocked disable restores the prior opt-in, so report the
                # persisted policy rather than assuming it is still enabled.
                try:
                    state = "enabled" if load_worker_settings(root).enabled else "disabled"
                except Exception:
                    state = None
                if state is None:
                    message = f"Worker disable was blocked ({diagnostic}); admission stays paused."
                else:
                    message = f"Worker remains {state} and admission-paused ({diagnostic})."
                print(message, file=sys.stderr)
            return 1
        payload = {"enabled": False, **result}
        _write(payload, as_json=args.json, human="Remote tasks worker disabled.")
        return 0
    if args.command == "lease" and args.lease_command == "release":
        try:
            ok, result, diagnostic = release_lease(
                root, args.provider, confirm_stopped=args.confirm_stopped
            )
        except ClaudeSwitchError:
            print("Worker lifecycle is busy; try again shortly.", file=sys.stderr)
            return 1
        if not ok:
            print(f"Lease was not released ({diagnostic}).", file=sys.stderr)
            if diagnostic == "stop_unproven_confirm_required":
                print(
                    "Stopping cannot be proven. Once no kickoff or provider process "
                    "is running for this account, rerun with --confirm-stopped.",
                    file=sys.stderr,
                )
            return 1
        payload = {"released": True, **result}
        human = (
            "Account lease was already released." if diagnostic == "already_released"
            else "Account lease released."
        )
        _write(payload, as_json=args.json, human=human)
        return 0
    parser.error("unsupported worker command")
    return 2


_ACCOUNT_MESSAGES = {
    "claude_not_supported": (
        "That names a Claude account. Use `claude:<slot>` to pick a Claude account explicitly."
    ),
    "account_not_found": "No account matches that. Run `openswap worker account` to list them.",
    "account_ambiguous": "That matches several accounts; use the slot number (claude:<slot> for Claude).",
    "account_not_eligible": (
        "That slot can't be pinned: a Codex API-key login has no ChatGPT account ID, "
        "and a Claude slot needs an email."
    ),
    "roster_unavailable": "The account roster could not be read.",
    "account_roster_busy": "Accounts are being changed; try again shortly.",
    "worker_lifecycle_busy": "Worker settings are being changed; try again shortly.",
    "settings_unavailable": "Could not save the worker settings.",
    "too_many_accounts": "At most 20 accounts can be allowed; disallow one first.",
    "account_not_allowlisted": (
        "That account is not allowed for a per-job choice. Run `openswap worker account` to list them."
    ),
    "account_is_default": (
        "That account is the pinned default. Pin another first, or pass --clear-default "
        "to disallow it and leave no default."
    ),
    "label_invalid": "Labels are 1-100 characters with no control characters.",
}

_WORKSPACE_MESSAGES = {
    "workspace_id_invalid": (
        "Workspace IDs are 1-64 characters: lowercase letters, digits, '-' and '_', "
        "starting with a letter or digit."
    ),
    "workspace_exists": "That workspace ID is already approved; remove it first to change its folder.",
    "workspace_not_found": "No approved workspace has that ID.",
    "last_workspace": "At least one research folder must stay approved; add another before removing this one.",
    "workspace_in_use": "A job using this research folder is still running or uploading its results; try again once it has finished.",
    "too_many_workspaces": "At most 16 research folders can be approved.",
    "too_many_readonly_sources": "At most 16 read-only sources can be approved per workspace.",
    "folder_invalid": "That folder path is not valid.",
    "folder_unavailable": "The folder could not be created or read.",
    "folder_unsafe": "The folder must be a real directory, not a symlink or a file.",
    "folder_permissions": (
        "The folder must be owned by you with no group or other access. "
        "Run `chmod 700` on it, then try again."
    ),
    "folder_exposes_credentials": (
        "That folder is, or contains, your home folder or an OpenSwap, Codex or Claude "
        "credential folder. Choose a dedicated research folder."
    ),
    "readonly_source_exposes_credentials": (
        "That read-only source overlaps your home folder or an OpenSwap, Codex or Claude "
        "credential folder."
    ),
    "readonly_source_unavailable": "A read-only source does not exist or cannot be read.",
    "readonly_source_unsafe": "A read-only source must be a real directory, not a symlink or a file.",
    "readonly_source_not_owned": "A read-only source must be owned by you.",
    "readonly_source_permissions": "A read-only source must not be writable by group or others.",
    "readonly_source_overlaps_folder": "The research folder and its read-only sources must not overlap.",
    "worker_lifecycle_busy": "Worker settings are being changed; try again shortly.",
    "settings_unavailable": "Could not save the worker settings.",
}


def _format_accounts(choices: AccountChoices) -> str:
    lines = ["Remote tasks account (a remote job runs on this pin's provider unless the "
             "service picks an allowed account):", "Codex:"]
    if not choices.codex:
        lines.append("  No Codex accounts saved. Add one with `openswap codex add`.")
    for choice in choices.codex:
        marker = "*" if choice.account_ref is not None and choice.account_ref == choices.pinned_ref else " "
        notes = []
        if choice.account_ref is not None and choice.account_ref == choices.pinned_ref:
            notes.append("pinned")
        if not choice.eligible:
            notes.append("not eligible: no ChatGPT account ID (API key)")
        if choice.disabled:
            notes.append("out of rotation")
        suffix = f"  [{'; '.join(notes)}]" if notes else ""
        lines.append(f"  {marker} {choice.label()}{suffix}")
    lines.append("Claude (pin with `claude:<slot>`):")
    if not choices.claude:
        lines.append("  No Claude accounts saved.")
    for entry in choices.claude:
        pinned = entry.account_ref is not None and entry.account_ref == choices.pinned_ref
        notes = ["pinned"] if pinned else []
        if not entry.eligible:
            notes.append("not eligible: no email")
        if entry.disabled:
            notes.append("out of rotation")
        suffix = f"  [{'; '.join(notes)}]" if notes else ""
        lines.append(f"  {'*' if pinned else ' '} {entry.label()}{suffix}")
    if choices.pinned_missing:
        lines.append(
            "The pinned account is no longer in its roster; jobs fail "
            "(provider_auth_unavailable) until you pin another."
        )
    elif choices.pinned_ref is None:
        lines.append("No account pinned: remote jobs that do not pick an allowed account fail until you pin one.")
    lines.append("Pin one with `openswap worker account <slot|email|alias>`; clear with `--clear`.")
    lines.append("Allowed for a per-job choice by the control service (* = default; the service "
                 "sees only the reference and label):")
    if not choices.allowlist:
        lines.append("  None. Allow one with `openswap worker account allow <slot|email|alias>`.")
    for entry in choices.allowlist:
        lines.append("  " + _format_allowlist_entry(entry, choices))
    lines.append("Manage with `openswap worker account allow|disallow|label`.")
    return "\n".join(lines)


def _format_allowlist_entry(entry: AllowlistedAccount, choices: AccountChoices) -> str:
    marker = "*" if entry.identity == choices.pinned_ref else " "
    slot = choices.slot_for(entry.identity)
    provider = "Claude" if entry.identity.startswith("claude:") else "Codex"
    where = f"{provider} slot {slot.number}" if slot is not None else f"no longer in the {provider} roster"
    return f"{marker} {entry.account_ref}  {json.dumps(entry.label, ensure_ascii=False)}  [{where}]"


def _allowlist_payload(entry: AllowlistedAccount, pinned_ref: str | None) -> dict:
    return {
        "account_ref": entry.account_ref, "identity": entry.identity,
        "label": entry.label, "default": entry.identity == pinned_ref,
    }


_ALLOWLIST_COMMANDS = {"allow", "disallow", "label"}


def _allowlist_command(root: Path, arguments: list[str]) -> int:
    """``openswap worker account allow|disallow|label``: the per-job choice allowlist."""
    parser = argparse.ArgumentParser(
        prog="openswap worker account",
        description="Manage the Codex and Claude accounts a control service may choose per job. Each "
                    "allowed account is advertised only as a random reference and a label you "
                    "choose (by default the slot alias or 'Codex account N' / 'Claude account N', "
                    "never the email).",
    )
    commands = parser.add_subparsers(dest="allowlist_command", required=True)
    allow = commands.add_parser("allow", help="allow a Codex or Claude account for a per-job choice")
    allow.add_argument("selector", metavar="SLOT|EMAIL|ALIAS")
    allow.add_argument("--label", default=None, help="label the control service shows (1-100 characters)")
    allow.add_argument("--json", action="store_true")
    disallow = commands.add_parser("disallow", help="withdraw an allowed account")
    disallow.add_argument("target", metavar="SLOT|EMAIL|ALIAS|REF")
    disallow.add_argument("--clear-default", action="store_true",
                          help="also clear the pin when the account is the pinned default")
    disallow.add_argument("--json", action="store_true")
    label = commands.add_parser("label", help="rename an allowed account's label")
    label.add_argument("target", metavar="SLOT|EMAIL|ALIAS|REF")
    label.add_argument("label", metavar="TEXT")
    label.add_argument("--json", action="store_true")
    args = parser.parse_args(arguments)
    try:
        if args.allowlist_command == "allow":
            entry = allow_worker_account(root, args.selector, args.label)
            human = f"Allowed {json.dumps(entry.label, ensure_ascii=False)} ({entry.account_ref}) for a per-job choice."
        elif args.allowlist_command == "disallow":
            entry = disallow_worker_account(root, args.target, clear_default=args.clear_default)
            human = f"Disallowed {json.dumps(entry.label, ensure_ascii=False)} ({entry.account_ref})."
        else:
            entry = label_worker_account(root, args.target, args.label)
            human = f"Relabelled {entry.account_ref} as {json.dumps(entry.label, ensure_ascii=False)}."
        pinned = load_worker_settings(root).pinned_account_ref
    except AccountPinError as exc:
        code = exc.code
    except ClaudeSwitchError as exc:
        code = str(exc) if str(exc) == "worker_lifecycle_busy" else "settings_unavailable"
    else:
        if args.allowlist_command == "disallow" and args.clear_default and pinned is None:
            human += " No default account is pinned now."
        payload = {"accepted": True, "account": _allowlist_payload(entry, pinned),
                   "pinned_account_ref": pinned}
        if args.allowlist_command == "disallow":
            payload["removed"] = True
            payload["account"]["default"] = False
        _write(payload, as_json=args.json, human=human)
        return 0
    if args.json:
        _write({"accepted": False, "diagnostic_code": code}, as_json=True)
    else:
        print(_ACCOUNT_MESSAGES.get(code, f"Could not change the allowed accounts ({code})."), file=sys.stderr)
    return 1


def _format_workspaces(workspaces) -> str:
    lines = ["Approved research folders (the ID is what the control service sends):"]
    for workspace in workspaces:
        lines.append(f"  {workspace.workspace_id}: {workspace.output_root}")
        for source in workspace.readonly_roots:
            lines.append(f"      read-only source: {source}")
    lines.append(
        "Add one with `openswap worker workspace add <id> <folder>`; the ID must match "
        "a Local workspace ID in the control service (OpenTag portal)."
    )
    return "\n".join(lines)


def _workspace_payload(workspace) -> dict:
    return {
        "workspace_id": workspace.workspace_id,
        "output_root": str(workspace.output_root),
        "readonly_roots": [str(root) for root in workspace.readonly_roots],
    }


def _choice_payload(choice) -> dict | None:
    if choice is None:
        return None
    return {
        "provider": choice.provider,
        "number": choice.number, "email": choice.email, "alias": choice.alias,
        "account_ref": choice.account_ref,
    }


def _interactive_terminal() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError, OSError):
        return False


_WORKSPACE_HINT = (
    "Then approve a research folder: `openswap worker workspace add <id> <folder>`. "
    "Use the same ID you enter as a Local workspace ID in the control service "
    "(the OpenTag portal); the folder path never leaves this Mac."
)


def _provider_name(choice) -> str:
    return "Claude" if getattr(choice, "provider", "codex") == "claude" else "Codex"


def _post_pair_setup(root: Path, *, interactive: bool, read_line=None) -> None:
    """After pairing: offer the account pin, then point at the folder step.

    Pairing has already succeeded; nothing here can undo or fail it.
    """
    read_line = read_line or input
    try:
        choices = worker_account_choices(root)
    except Exception:
        choices = None
    if choices is not None and choices.pinned_ref is not None and not choices.pinned_missing:
        print(f"Remote tasks uses {_provider_name(choices.pinned)} account {choices.pinned.label()}.")
    elif not interactive or choices is None:
        print("Next: pin the account remote jobs run on: `openswap worker account <slot|email|alias>` "
              "(`claude:<slot>` for Claude; `openswap worker account` lists them).")
    else:
        eligible = [choice for choice in choices.codex if choice.eligible]
        eligible_claude = [choice for choice in choices.claude if choice.eligible]
        if not eligible and not eligible_claude:
            print("No eligible account is saved. Add one with `openswap codex add` or `openswap add`, then "
                  "`openswap worker account <slot>`.")
        else:
            print("Choose the account remote jobs run on:")
            for choice in eligible:
                print(f"  {choice.label()}  (Codex)")
            for choice in eligible_claude:
                print(f"  claude:{choice.label()}  (Claude)")
            for _attempt in range(3):
                try:
                    answer = read_line("Account (slot, email or alias; claude:<slot> for Claude; Enter to skip): ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    answer = ""
                if not answer:
                    print("Skipped. Pin one later with `openswap worker account <slot|email|alias>`.")
                    break
                try:
                    pinned = set_worker_account(root, answer)
                except AccountPinError as exc:
                    print(_ACCOUNT_MESSAGES.get(exc.code, f"Could not pin that account ({exc.code})."))
                    continue
                except Exception:
                    print("Could not pin that account. Try `openswap worker account <slot>` later.")
                    break
                print(f"Pinned {_provider_name(pinned)} account {pinned.label()} for Remote tasks.")
                break
            else:
                print("Pin one later with `openswap worker account <slot|email|alias>`.")
    print(_WORKSPACE_HINT)


def _account_command(root: Path, args) -> int:
    if args.clear and args.selector is not None:
        print("Pass an account or --clear, not both.", file=sys.stderr)
        return 2
    if args.selector is None and not args.clear:
        try:
            choices = worker_account_choices(root)
        except Exception:
            print("Worker account settings unavailable.", file=sys.stderr)
            return 1
        _write(choices.to_dict(), as_json=args.json, human=_format_accounts(choices))
        return 0
    try:
        choice = set_worker_account(root, None if args.clear else args.selector)
    except AccountPinError as exc:
        code = exc.code
    except ClaudeSwitchError as exc:
        code = str(exc) if str(exc) == "worker_lifecycle_busy" else "settings_unavailable"
    else:
        payload = {"accepted": True, "pinned": _choice_payload(choice)}
        human = (
            "Cleared the Remote tasks account; remote jobs that do not pick an allowed account "
            "fail until you pin one."
            if choice is None
            else f"Remote tasks will use {'Claude' if choice.provider == 'claude' else 'Codex'} "
                 f"account {choice.label()} from the next job."
        )
        _write(payload, as_json=args.json, human=human)
        return 0
    if args.json:
        _write({"accepted": False, "diagnostic_code": code}, as_json=True)
    else:
        print(_ACCOUNT_MESSAGES.get(code, f"Could not pin that account ({code})."), file=sys.stderr)
    return 1


def _workspace_command(root: Path, args) -> int:
    try:
        if args.workspace_command == "list":
            workspaces = load_worker_settings(root).workspaces
            _write(
                {"workspaces": [_workspace_payload(item) for item in workspaces]},
                as_json=args.json, human=_format_workspaces(workspaces),
            )
            return 0
        if args.workspace_command == "add":
            workspace = add_worker_workspace(
                root, args.workspace_id, args.folder, tuple(args.readonly_source or ()),
            )
            _write(
                {"accepted": True, "workspace": _workspace_payload(workspace)},
                as_json=args.json,
                human=f"Approved research folder {workspace.output_root} as workspace "
                      f"'{workspace.workspace_id}'.",
            )
            return 0
        remove_worker_workspace(root, args.workspace_id)
        _write(
            {"accepted": True, "removed": args.workspace_id}, as_json=args.json,
            human=f"Removed workspace '{args.workspace_id}'; its folder and files are unchanged.",
        )
        return 0
    except WorkspaceError as exc:
        code = exc.code
    except ClaudeSwitchError as exc:
        code = str(exc) if str(exc) == "worker_lifecycle_busy" else "settings_unavailable"
    except (OSError, RuntimeError, ValueError):
        code = "settings_unavailable"
    if args.json:
        _write({"accepted": False, "diagnostic_code": code}, as_json=True)
    else:
        print(_WORKSPACE_MESSAGES.get(code, f"Workspace change refused ({code})."), file=sys.stderr)
    return 1


def _enable_failure_message(exc: ClaudeSwitchError) -> str:
    """What `openswap worker enable` prints when enable_worker refuses."""
    code = str(exc)
    if code == "worker_configuration_invalid":
        return "Worker configuration is invalid; fix local worker settings before enabling."
    if code == "worker_stop_unconfirmed":
        return "Worker is still stopping; wait for it to exit before enabling."
    return "Could not enable worker."


# enable_worker's own refusal codes. Anything else (for example launchctl or
# file-system detail from installing the LaunchAgent) is not echoed.
_ENABLE_DIAGNOSTICS = frozenset({
    "worker_configuration_invalid", "worker_stop_unconfirmed", "worker_running_unmanaged",
    "kickoff_in_progress", "worker_lifecycle_busy", "worker_state_unavailable",
})

_START_WORKER_NEXT = (
    "Next: start the Remote tasks worker so this Mac can accept approved tasks: "
    "`openswap worker enable`."
)
_EXECUTION_OFF_NOTE = (
    "Task execution itself stays off (provider: live_adapter_disabled) until you run "
    "`openswap worker live-check` (add `--provider claude` for a Claude account) on this Mac and enable "
    "live execution, so jobs are refused for now."
)
_RUNNING_STATES = frozenset({"starting", "running"})


def _post_pair_worker_offer(root: Path, *, interactive: bool, read_line=None) -> None:
    """After pairing: offer to start the worker, through `worker enable`'s own path.

    Enrollment never enables local execution by itself: the worker starts only
    when the owner answers yes on a terminal. Pairing has already succeeded;
    nothing here can undo or fail it.
    """
    read_line = read_line or input
    enabled = load_worker_settings(root).enabled is True
    if enabled:
        try:
            process = read_status(root).get("process_state")
        except Exception:
            process = None
        if process in _RUNNING_STATES:
            print("The Remote tasks worker is already running on this Mac.")
        else:
            # Enabled but not running (for example its LaunchAgent was
            # unloaded). Report it; never toggle anything from here.
            print("The Remote tasks worker is enabled but not running. Run `openswap worker enable` "
                  "to start it again, or `openswap worker status` to check.")
    elif not interactive:
        print(_START_WORKER_NEXT)
    else:
        try:
            answer = read_line(
                "Start the Remote tasks worker now so this Mac can accept approved tasks? [Y/n] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = None
        if answer not in {"", "y", "yes"}:
            print("Not started. Start it later with `openswap worker enable`.")
        else:
            try:
                enable_worker(root)
            except ClaudeSwitchError as exc:
                code = str(exc)
                message = _enable_failure_message(exc)
                if code in _ENABLE_DIAGNOSTICS:
                    message = f"{message.rstrip('.')} ({code})."
                print(f"{message} Start it later with `openswap worker enable`.")
            except Exception:
                print("Could not enable worker. Start it later with `openswap worker enable`.")
            else:
                print("Remote tasks worker enabled. The portal shows this Mac online within about 15 seconds.")
    from openswap.worker.live import LIVE, pinned_execution_mode

    if pinned_execution_mode(root) != LIVE:
        print(_EXECUTION_OFF_NOTE)


def _worker_off_hint(root: Path, snapshot: dict) -> str | None:
    """One line for `worker status` when paired but the worker is not running."""
    try:
        url = load_worker_settings(root).control_service_url
    except Exception:
        return None
    if url is None:
        return None
    if snapshot.get("enabled") is True and snapshot.get("process_state") in _RUNNING_STATES:
        return None
    return f"Paired with {url} but the worker is off; run `openswap worker enable`."


def _format_status(snapshot: dict) -> str:
    enabled = "enabled" if snapshot.get("enabled") is True else "disabled"
    process = snapshot.get("process_state", "unavailable")
    admission = "paused" if snapshot.get("paused") is True else "open"
    provider = snapshot.get("provider") or {}
    provider_state = (
        "available" if provider.get("available") is True
        else provider.get("diagnostic_code") or "unavailable"
    )
    remote = snapshot.get("remote_connectivity", "disabled")
    seen = snapshot.get("remote_last_seen_at") or "never"
    active = snapshot.get("active_job")
    job = f"; job {active.get('job_id')} ({active.get('state')})" if active else ""
    return (
        f"Remote tasks: {enabled}; worker: {process}; admission: {admission}; "
        f"provider: {provider_state}; service: {remote}; service last seen: {seen}{job}"
    )
