"""Early, credential-free CLI boundary for local worker operations."""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from openswap import paths, pathid, printer
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
    """Allowlist a Codex or Claude account so a control service may choose it per job.

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
            out.append(pathid.canonical(Path(path).expanduser()))
        except (OSError, RuntimeError):
            continue
    return out


def _homes() -> list[Path]:
    out = []
    for home in (Path.home(), home_folder()):
        try:
            home = pathid.canonical(Path(home).expanduser())
        except (OSError, RuntimeError):
            continue
        if home not in out:
            out.append(home)
    return out


def _overlaps_credentials(root: Path, folder: Path, *, writable: bool) -> bool:
    """Whether a folder would expose OpenSwap or provider credential homes.

    No approved folder may be the home directory, or contain the backup root,
    Codex home or Claude config home. A read-only source may not sit inside
    one either. A writable root may sit inside the backup root's ``worker``
    directory only (where the default ``research`` workspace lives). Paths
    are compared by on-disk spelling and identity (``pathid``), so a case
    variant on a case-insensitive volume is the same folder.
    """
    protected = _protected_dirs(root)
    for path in [*protected, *_homes()]:
        if pathid.inside(path, folder):
            return True
    try:
        backup = pathid.canonical(Path(root).expanduser())
    except (OSError, RuntimeError):
        backup = None
    for path in protected:
        if pathid.inside(folder, path):
            # Only the backup root itself has the `worker` exception: a Codex or
            # Claude home configured beneath that directory stays off limits.
            if (writable and backup is not None and pathid.same(path, backup)
                    and pathid.inside(folder, backup / "worker")):
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


DEFAULT_RESEARCH_ID = "research"
DEFAULT_RESEARCH_FOLDER_NAME = "OpenSwap Research"
# Where people usually keep code, relative to the home folder. A GitHub folder
# is listed (and offered) first; the rest keep this order.
CODE_FOLDER_NAMES = ("GitHub", "Documents/GitHub", "Developer", "Code", "Projects", "src", "repos", "dev")
# A detected folder with this many git repos or fewer also offers each repo.
_FEW_REPOS = 3
_MAX_SCANNED_CHILDREN = 200
MAX_SUGGESTED_FOLDERS = 12
# Never readable: system trees (and anything inside them), and these top
# folders themselves or anything that contains them.
_SYSTEM_TREES = tuple(Path(p) for p in (
    "/System", "/Library", "/usr", "/bin", "/sbin", "/etc", "/private/etc", "/dev", "/Applications",
    "/cores", "/Network",
))
_SYSTEM_TOPS = tuple(Path(p) for p in (
    "/", "/private", "/var", "/private/var", "/tmp", "/private/tmp", "/Users", "/Volumes", "/opt", "/home",
))


def home_folder() -> Path:
    """The owner's home folder (a seam for tests)."""
    return Path.home()


def display_path(path: str | Path) -> str:
    """A path for the terminal: ``~`` for the home folder (as given, or resolved)."""
    path = Path(path)
    home = home_folder()
    for base in (home, _resolved(home)):
        if base is None:
            continue
        try:
            relative = path.relative_to(base)
        except ValueError:
            continue
        return "~/" + relative.as_posix() if relative.parts else "~"
    return str(path)


def default_research_folder() -> Path:
    """Where task results are saved: ``~/OpenSwap Research``, one folder per workspace ID."""
    return home_folder() / DEFAULT_RESEARCH_FOLDER_NAME


def is_builtin_default_registry(backup_root: Path, workspaces) -> bool:
    """Whether the registry is still only the built-in ``research`` folder.

    Settings without an approved folder fall back to ``research`` inside the
    OpenSwap backup root (and the first ``worker enable`` writes that down).
    The guided setup replaces it with the first folder the owner chooses.
    """
    from openswap.settings import _default_worker_workspace

    return tuple(workspaces) == (_default_worker_workspace(Path(backup_root)),)


def _valid_workspace_label(label: str | None) -> str | None:
    if label is None:
        return None
    if not valid_account_label(label):
        raise WorkspaceError("label_invalid")
    return label


def suggested_folder_id(folder: Path, taken: set[str]) -> str:
    """A free workspace ID from a folder's name: lowercase letters, digits, '-' and '_'.

    Anything else (a dot, a space, an accented letter) becomes '-', so the ID
    is also one of the control service's ``[A-Za-z0-9_.-]`` folder IDs.
    """
    base = re.sub(r"[^a-z0-9_-]+", "-", Path(folder).name.lower()).strip("-_")[:56] or "folder"
    candidate, number = base, 2
    while candidate in taken:
        candidate, number = f"{base}-{number}", number + 1
    return candidate


def _resolved(path: Path) -> Path | None:
    try:
        return pathid.canonical(Path(path).expanduser())
    except (OSError, RuntimeError):
        return None


def readable_folder_problem(backup_root: Path, folder: Path) -> str | None:
    """Why remote tasks may not read ``folder`` (absolute), or ``None``.

    ``system`` (a system folder, or one containing them), ``home`` (the home
    folder or a folder containing it), ``private`` (``~/Library`` or a hidden
    folder in home), ``exposes_credentials`` (overlaps the OpenSwap backup
    root, Codex home or Claude config home), ``results`` (overlaps
    ``~/OpenSwap Research``, where results are written), then the read-only
    source checks the worker repeats at launch: ``unavailable``, ``unsafe``,
    ``not_owned`` or ``permissions``.

    Every comparison goes through ``pathid`` (on-disk spelling and identity),
    so ``~/library`` on a case-insensitive volume is ``~/Library``.
    """
    folder = pathid.canonical(folder)
    if (any(pathid.inside(top, folder) for top in _SYSTEM_TOPS)
            or any(pathid.inside(folder, tree) for tree in _SYSTEM_TREES)):
        return "system"
    for home in _homes():
        if pathid.inside(home, folder):
            return "home"
        first = pathid.top_component(folder, home)
        if first is not None and first.startswith("."):
            return "private"
        if first is not None and first.casefold() == "library" and not _in_cloud_drive(folder, home / first):
            return "private"
    if _overlaps_credentials(backup_root, folder, writable=False):
        return "exposes_credentials"
    results = _resolved(default_research_folder())
    if results is not None and pathid.overlap(folder, results):
        return "results"
    return readonly_source_problem(folder)


def _in_cloud_drive(folder: Path, library: Path) -> bool:
    """Whether ``folder`` is a synced cloud drive inside ``~/Library`` (or a folder in one).

    A provider's folder in ``~/Library/CloudStorage`` (Dropbox, Google Drive,
    OneDrive, ...) and iCloud Drive (``~/Library/Mobile Documents/com~apple~CloudDocs``)
    hold the owner's own files. Never the ``CloudStorage`` or ``Mobile
    Documents`` folders themselves, nor anything else in ``~/Library``.
    ``folder`` is canonical (symlinks resolved, on-disk spelling), so its own
    components are compared with the exact on-disk names below the canonical
    ``~/Library``: the bases are never canonicalised themselves, so a
    ``CloudStorage`` that is a symlink to ``~/Library`` (or anywhere else)
    widens nothing, and a base that is a symlink is refused outright.
    """
    library = pathid.canonical(library)
    try:
        parts = Path(folder).relative_to(library).parts
    except ValueError:
        return False

    def real_dirs(*names: str) -> bool:
        base = library
        for name in names:
            base = base / name
            if base.is_symlink() or not base.is_dir():
                return False
        return True

    if len(parts) >= 2 and parts[0] == "CloudStorage" and not parts[1].startswith("."):
        return real_dirs("CloudStorage")
    if parts[:2] == ("Mobile Documents", "com~apple~CloudDocs"):
        return real_dirs("Mobile Documents", "com~apple~CloudDocs")
    return False


def _source_problem_code(problem: str) -> str:
    """The workspace error code for a read-only source refused by ``readable_folder_problem``."""
    if problem in {"unavailable", "unsafe", "not_owned", "permissions", "exposes_credentials"}:
        return f"readonly_source_{problem}"
    return f"readable_{problem}"


def workspace_refusal(backup_root: Path, workspace, workspaces) -> str | None:
    """Why the worker refuses jobs in ``workspace`` at launch, as a workspace error code, or ``None``.

    The rules ``workspace add`` applies at approval, checked again for what
    settings hold now (saved before a rule existed, or edited by hand): every
    read-only source must pass ``readable_folder_problem``, and no workspace
    may read a folder another one writes to, or write inside a folder another
    one reads.
    """
    root = Path(backup_root)
    # The results folder (created at launch when missing): an existing one
    # that became reachable by others, or was replaced by a file or a link,
    # refuses every job, so it is diagnosed and withheld here too.
    problem = _results_problem(root, workspace.output_root)
    if problem is not None:
        return problem
    for source in workspace.readonly_roots:
        problem = readable_folder_problem(root, source)
        if problem is not None:
            return _source_problem_code(problem)
    work = getattr(workspace, "work_root", None)
    if work is not None and workspace.mode == "worktree" and not workspace.repos:
        # Where the task's worktree goes: owner-only too.
        base = default_research_folder() / ".worktrees"
        problem = _results_problem(root, base) or _results_problem(root, base / workspace.workspace_id)
        if problem is not None:
            return problem
    if work is not None:
        problem = work_folder_problem(root, work, workspace.mode, workspace.repos)
        if problem is not None:
            return _work_problem_code(problem)
    for other in workspaces:
        if other.workspace_id == workspace.workspace_id:
            continue
        if any(pathid.overlap(source, other.output_root) for source in workspace.readonly_roots):
            return "readonly_source_overlaps_results"
        if any(pathid.overlap(workspace.output_root, source) for source in other.readonly_roots):
            return "folder_overlaps_readable"
        if work is not None and pathid.overlap(work, other.output_root):
            return "work_overlaps_results"
        other_work = getattr(other, "work_root", None)
        if other_work is not None and pathid.overlap(workspace.output_root, other_work):
            return "folder_overlaps_work"
        # A folder sessions may change is never one another workspace only reads.
        if work is not None and any(pathid.overlap(work, source) for source in other.readonly_roots):
            return "work_overlaps_readable"
        if other_work is not None and any(pathid.overlap(source, other_work) for source in workspace.readonly_roots):
            return "work_overlaps_readable"
    return None


def _results_problem(root: Path, folder: Path) -> str | None:
    """Why an existing results (or worktree) folder cannot take a task, as a workspace error code."""
    if not os.path.lexists(folder):
        return None
    if _overlaps_credentials(root, pathid.canonical(folder), writable=True):
        return "folder_exposes_credentials"
    problem = output_dir_problem(Path(folder))
    return None if problem is None else f"folder_{problem}"


def work_folder_problem(backup_root: Path, folder: Path, mode: str = "worktree", repos: bool = False) -> str | None:
    """Why a remote session may not work in ``folder``, or ``None``.

    The folder policy of ``readable_folder_problem`` (never the home folder,
    a system or private folder, a credential home or the results folder;
    owned by the owner and not writable by others), and in worktree mode a
    git repo (``not_a_repo``), or for a folder of repos at least one repo in
    it (``no_repos``).
    """
    from openswap.worker import worktrees

    problem = readable_folder_problem(Path(backup_root), folder)
    if problem is not None:
        return problem
    # A folder of repos needs a repo in it whatever its repos' mode.
    if repos:
        return None if worktrees.child_repos(folder) else "no_repos"
    if mode == "worktree" and not worktrees.is_repo(folder):
        return "not_a_repo"
    return None


def _work_problem_code(problem: str) -> str:
    return f"work_{problem}" if problem in {"not_a_repo", "no_repos"} else f"readable_{problem}"


def _repo_ids_file(root: Path) -> Path:
    return Path(root) / "worker" / "repo-ids.json"


def repo_ids(backup_root: Path) -> dict[str, str]:
    """Repo path -> ID for every repo ever offered from a folder of repos (never reassigned)."""
    try:
        data = json.loads(_repo_ids_file(backup_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    ids = data.get("ids") if isinstance(data, dict) else None
    if not isinstance(ids, dict):
        return {}
    from openswap.settings import _WORKSPACE_ID_RE

    return {path: workspace_id for path, workspace_id in ids.items()
            if isinstance(path, str) and isinstance(workspace_id, str) and _WORKSPACE_ID_RE.fullmatch(workspace_id)}


def _assign_repo_ids(root: Path, repos: list[Path], static_ids: set[str]) -> dict[str, str]:
    """IDs for ``repos``: the one each was first given, or a new free one, saved for good.

    A repo keeps its ID however repos are later added, removed or renamed
    around it, so a task already sent for ``foo-bar`` can never land in a
    different repo. New IDs avoid every approved ID and every ID ever given.
    Returns only the repos whose IDs are saved (a repo whose ID cannot be
    saved is not offered).
    """
    from openswap.worker.journal import LocalJobStore

    known = repo_ids(root)
    wanted = [repo for repo in repos if str(repo) not in known]
    if wanted:
        try:
            LocalJobStore(root)._ensure_private_dir()
            with FileLock(Path(root) / "worker" / "repo-ids.lock", timeout=_LIFECYCLE_LOCK_TIMEOUT_SECONDS):
                known = repo_ids(root)
                taken = set(static_ids) | set(known.values())
                for repo in wanted:
                    if str(repo) not in known:
                        known[str(repo)] = suggested_folder_id(repo, taken)
                        taken.add(known[str(repo)])
                path = _repo_ids_file(root)
                temporary = path.with_name(path.name + ".tmp")
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump({"version": 1, "ids": known}, stream, sort_keys=True)
                os.replace(temporary, path)
        except Exception:
            known = repo_ids(root)
    return {str(repo): known[str(repo)] for repo in repos if str(repo) in known}


def launchable_workspaces(backup_root: Path, workspaces) -> tuple:
    """The workspaces a task may name: each approved one, with a folder of repos
    replaced by one work folder per git repo directly inside it.

    Scanned again on every call (each launch and readiness report), so a new
    repo appears without changing settings. A repo that is already a work
    folder of its own is not listed twice. A repo's ID comes from its name
    the first time it is seen and is kept for good (``repo-ids.json``), so it
    never moves to another repo; an approved ID always wins over it.
    """
    from dataclasses import replace

    from openswap.worker import worktrees

    static = [w for w in workspaces if not getattr(w, "repos", False)]
    static_ids = {w.workspace_id for w in workspaces}
    out = list(static)
    results = default_research_folder()
    for parent in workspaces:
        if not getattr(parent, "repos", False) or parent.work_root is None:
            continue
        known = set(repo_ids(Path(backup_root)))
        repos = [repo for repo in worktrees.child_repos(parent.work_root, keep=known)
                 if not any(w.work_root is not None and pathid.same(w.work_root, repo) for w in out)]
        assigned = _assign_repo_ids(Path(backup_root), repos, static_ids)
        for repo in repos:
            workspace_id = assigned.get(str(repo))
            if workspace_id is None or workspace_id in static_ids or any(w.workspace_id == workspace_id for w in out):
                continue
            label = repo.name if valid_account_label(repo.name) else None
            out.append(replace(parent, workspace_id=workspace_id, output_root=results / workspace_id,
                               readonly_roots=(), label=label, work_root=repo, repos=False))
    return tuple(out)


def results_folder(backup_root: Path, workspace_id: str, workspaces) -> Path | None:
    """Where a workspace's task results live, even if its repo is gone since the task ran."""
    from openswap.settings import _WORKSPACE_ID_RE

    static = next((w for w in workspaces if w.workspace_id == workspace_id and not w.repos), None)
    if static is not None:
        return static.output_root
    if _WORKSPACE_ID_RE.fullmatch(workspace_id) and workspace_id in set(repo_ids(backup_root).values()):
        return default_research_folder() / workspace_id
    return None


def refused_workspaces(backup_root: Path, workspaces=None) -> list[tuple[str, str]]:
    """``(workspace ID, code)`` for each approved workspace whose jobs are refused at launch.

    Without ``workspaces`` it checks what settings hold: every launchable
    workspace, and every saved folder of repos too. Such a folder is never
    itself launchable, so one that became unavailable or lost all its repos
    would otherwise vanish from the report with nothing said.
    """
    checked = workspaces
    if workspaces is None:
        saved = load_worker_settings(Path(backup_root)).workspaces
        workspaces = launchable_workspaces(backup_root, saved)
        checked = (*workspaces, *(w for w in saved if w.repos))
    out = []
    for workspace in checked:
        try:
            code = workspace_refusal(backup_root, workspace, workspaces)
        except Exception:
            code = "settings_unavailable"
        if code is not None:
            out.append((workspace.workspace_id, code))
    return out


def refusal_line(workspace_id: str, code: str) -> str:
    """One path-free line saying which folder is refused at launch and why."""
    reason = _WORKSPACE_MESSAGES.get(code, "It breaks the folder rules.")
    return f'{printer.MARK_BAD} "{workspace_id}" is blocked ({code}). {reason}'


def is_github_folder(folder: Path) -> bool:
    return Path(folder).name.lower() == "github"


def _few_git_repos(parent: Path) -> list[Path]:
    """The git repos directly inside ``parent`` when there are only a few, else none."""
    repos = []
    try:
        children = sorted(parent.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    if len(children) > _MAX_SCANNED_CHILDREN:
        return []
    for child in children:
        try:
            if child.name.startswith(".") or child.is_symlink() or not child.is_dir():
                continue
            if not (child / ".git").exists():
                continue
        except OSError:
            continue
        repos.append(child)
        if len(repos) > _FEW_REPOS:
            return []
    return repos


def detect_code_folders(backup_root: Path) -> list[Path]:
    """Likely code folders in the home folder that tasks may read, GitHub first.

    Only existing folders that pass ``readable_folder_problem`` are kept,
    resolved and without duplicates. A folder holding only a few git repos
    is followed by those repos, so the owner can pick just one.
    """
    home = home_folder()
    found: list[Path] = []
    for name in CODE_FOLDER_NAMES:
        candidate = home / name
        try:
            if not candidate.is_dir():
                continue
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        resolved = pathid.canonical(resolved)
        if (not any(pathid.same(resolved, other) for other in found)
                and readable_folder_problem(backup_root, resolved) is None):
            found.append(resolved)
    found.sort(key=lambda folder: not is_github_folder(folder))  # stable: GitHub first
    out: list[Path] = []
    for parent in found:
        if parent not in out:
            out.append(parent)
        for repo in _few_git_repos(parent):
            repo = _resolved(repo)
            if (repo is not None and not any(pathid.same(repo, other) for other in out)
                    and readable_folder_problem(backup_root, repo) is None):
                out.append(repo)
    return out[:MAX_SUGGESTED_FOLDERS]


def add_worker_workspace(
    backup_root: Path,
    workspace_id: str,
    folder: str | Path,
    readonly_sources: tuple[str | Path, ...] = (),
    *,
    label: str | None = None,
    replace_builtin_default: bool = False,
) -> WorkerWorkspace:
    """Approve ``folder`` as the writable research root for ``workspace_id``.

    The ID is the opaque name a remote submission uses; with the label (by
    default the folder's name) it is all the control service learns, through
    the readiness report. The path never leaves this Mac. A missing folder is
    created owner-only (0700). The folder must pass the same checks the
    worker applies at launch: a real directory owned by this user with no
    group or other access. Read-only sources must exist, be owned by this
    user and not be writable by others.

    With ``replace_builtin_default``, a registry that is still only the
    built-in ``research`` folder (see ``is_builtin_default_registry``) is
    replaced instead of extended, only while no job may still run or upload
    in it.
    """
    from openswap.settings import _WORKSPACE_ID_RE

    root = Path(backup_root)
    if not isinstance(workspace_id, str) or not _WORKSPACE_ID_RE.fullmatch(workspace_id):
        raise WorkspaceError("workspace_id_invalid")
    label = _valid_workspace_label(label)
    output = _absolute_folder(folder)
    sources = tuple(_absolute_folder(item) for item in readonly_sources)
    if len(sources) > 16:
        raise WorkspaceError("too_many_readonly_sources")
    _migrate_legacy_before_worker_state_change(root)
    with lifecycle_lock(root):
        current = load_worker_settings(root)
        kept = current.workspaces
        if replace_builtin_default and is_builtin_default_registry(root, current.workspaces):
            if _workspace_in_use(root, current.workspaces[0].workspace_id):
                raise WorkspaceError("workspace_in_use")
            kept = ()
        return _approve_locked(root, kept, workspace_id, output, sources, label)


def _approve_locked(root: Path, kept, workspace_id: str, output: Path, sources, label, *,
                    work_root: Path | None = None, mode: str = "worktree", repos: bool = False,
                    replacing: str | None = None) -> WorkerWorkspace:
    """Check and save one more workspace after ``kept``; call under the lifecycle lock.

    An ID already given to a repo in a folder of repos is taken.

    ``replacing`` names a workspace in ``kept`` this one takes the place of
    (same ID, same position), as when a read-only folder becomes a work folder.
    """
    if replacing is not None:
        position = next(i for i, item in enumerate(kept) if item.workspace_id == replacing)
        before, after = kept[:position], kept[position + 1:]
        kept = (*before, *after)
    else:
        before, after = kept, ()
    if any(item.workspace_id == workspace_id for item in kept) or (
            replacing is None and workspace_id in set(repo_ids(root).values())):
        raise WorkspaceError("workspace_exists")
    if len(kept) >= 16:
        raise WorkspaceError("too_many_workspaces")
    try:
        output.mkdir(mode=0o700, parents=True, exist_ok=True)
        output = pathid.canonical(output)
        sources = tuple(pathid.canonical(source) for source in sources)
    except (OSError, RuntimeError):
        raise WorkspaceError("folder_unavailable") from None
    if _overlaps_credentials(root, output, writable=True):
        raise WorkspaceError("folder_exposes_credentials")
    problem = output_dir_problem(output)
    if problem is not None:
        raise WorkspaceError(f"folder_{problem}")
    for source in sources:
        # The same policy as a folder chosen in the guided setup: never the
        # home folder, a system folder, ~/Library, a hidden folder or a
        # credential home.
        problem = readable_folder_problem(root, source)
        if problem is not None:
            raise WorkspaceError(_source_problem_code(problem))
        if pathid.overlap(source, output):
            raise WorkspaceError("readonly_source_overlaps_folder")
    if work_root is not None:
        work_root = pathid.canonical(work_root)
        problem = work_folder_problem(root, work_root, mode, repos)
        if problem is not None:
            raise WorkspaceError(_work_problem_code(problem))
        if pathid.overlap(work_root, output):
            raise WorkspaceError("work_overlaps_results")
    # Across workspaces too: no job may write where another one only reads,
    # and no task's results land inside a folder where sessions work.
    for other in kept:
        if any(pathid.overlap(source, other.output_root) for source in sources):
            raise WorkspaceError("readonly_source_overlaps_results")
        if any(pathid.overlap(output, source) for source in other.readonly_roots):
            raise WorkspaceError("folder_overlaps_readable")
        if work_root is not None and pathid.overlap(work_root, other.output_root):
            raise WorkspaceError("work_overlaps_results")
        if other.work_root is not None and pathid.overlap(output, other.work_root):
            raise WorkspaceError("folder_overlaps_work")
        if work_root is not None and any(pathid.overlap(work_root, source) for source in other.readonly_roots):
            raise WorkspaceError("work_overlaps_readable")
        if other.work_root is not None and any(pathid.overlap(source, other.work_root) for source in sources):
            raise WorkspaceError("work_overlaps_readable")
    workspace = WorkerWorkspace(workspace_id, output, sources, label, work_root, mode, repos)
    try:
        set_worker_workspaces(root, (*before, workspace, *after) if replacing is not None else (*kept, workspace))
    except ValueError as exc:
        if "disjoint" in str(exc):
            raise WorkspaceError("readonly_source_overlaps_folder") from None
        raise WorkspaceError("settings_unavailable") from None
    except (OSError, RuntimeError):
        raise WorkspaceError("settings_unavailable") from None
    return workspace


@dataclass(frozen=True)
class ReadableFolder:
    """What ``add_readable_folder`` did."""

    workspace: WorkerWorkspace
    added: bool  # False: a workspace of its own already reads this folder
    kept_builtin: bool = False  # the built-in folder stayed beside it: a job may still use it


def readable_workspace(workspaces, folder: Path) -> WorkerWorkspace | None:
    """The approved workspace whose only read-only source is ``folder`` (by identity), if any."""
    return next((w for w in workspaces
                 if len(w.readonly_roots) == 1 and pathid.same(w.readonly_roots[0], folder)), None)


def add_readable_folder(backup_root: Path, folder: str | Path, *, label: str | None = None) -> ReadableFolder:
    """Let remote tasks read ``folder`` but never change it, as the guided setup does.

    The workspace ID comes from the folder's name (made free), the label is
    the folder's name, the folder is the only read-only source, and results
    go to ``~/OpenSwap Research/<id>``, created owner-only (0700). The first
    one replaces the built-in ``research`` folder, unless a job may still run
    or upload in it (then that stays beside it). A folder already read by a
    workspace of its own is left as it is.
    """
    root = Path(backup_root)
    text = str(folder)
    if text == "~" or text.startswith("~/"):
        folder = home_folder() / text[2:].lstrip("/")
    try:
        source = pathid.canonical(_absolute_folder(folder).resolve(strict=True))
    except (OSError, RuntimeError):
        raise WorkspaceError("readable_unavailable") from None
    problem = readable_folder_problem(root, source)
    if problem is not None:
        raise WorkspaceError(f"readable_{problem}")
    if label is None and valid_account_label(source.name):
        label = source.name
    label = _valid_workspace_label(label)
    _migrate_legacy_before_worker_state_change(root)
    with lifecycle_lock(root):
        current = load_worker_settings(root)
        existing = readable_workspace(current.workspaces, source)
        if existing is not None:
            return ReadableFolder(existing, added=False)
        kept, kept_builtin = current.workspaces, False
        if is_builtin_default_registry(root, kept):
            if _workspace_in_use(root, kept[0].workspace_id):
                kept_builtin = True
            else:
                kept = ()
        workspace_id = suggested_folder_id(source, {w.workspace_id for w in kept} | set(repo_ids(root).values()))
        results = default_research_folder()
        try:
            results.mkdir(mode=0o700, exist_ok=True)
        except OSError:
            raise WorkspaceError("folder_unavailable") from None
        workspace = _approve_locked(root, kept, workspace_id, results / workspace_id, (source,), label)
        return ReadableFolder(workspace, added=True, kept_builtin=kept_builtin)


@dataclass(frozen=True)
class WorkFolder:
    """What ``add_work_folder`` did."""

    workspace: WorkerWorkspace
    added: bool  # False: sessions could already work there under this ID
    kept_builtin: bool = False
    repos: tuple[str, ...] = ()  # for a folder of repos: the IDs its repos are offered under


def work_workspace(workspaces, folder: Path) -> WorkerWorkspace | None:
    """The workspace whose sessions work in ``folder`` (by identity), if any."""
    return next((w for w in workspaces if w.work_root is not None and pathid.same(w.work_root, folder)), None)


def _expand_folder(path: str | Path) -> Path:
    text = str(path)
    if text == "~" or text.startswith("~/"):
        return home_folder() / text[2:].lstrip("/")
    return Path(path)


def add_work_folder(backup_root: Path, folder: str | Path, *, mode: str = "worktree",
                    label: str | None = None) -> WorkFolder:
    """Let remote sessions work in ``folder``, as the guided setup does.

    A git repo becomes one work folder (ID from its name). A folder that is
    not a repo but holds repos (``~/GitHub``) offers each repo directly
    inside it as a work folder of its own (see ``launchable_workspaces``).
    In the default worktree mode a folder with neither is refused: there is
    nothing to make a task's own copy of. ``mode="direct"`` (CLI only) lets
    sessions work in the folder itself.

    Results still go to ``~/OpenSwap Research/<id>``. A folder that was
    added before as a read-only folder keeps its ID and becomes a work
    folder. The built-in ``research`` folder is replaced as by
    ``add_readable_folder``.
    """
    from openswap.settings import WORK_MODES
    from openswap.worker import worktrees

    if mode not in WORK_MODES:
        raise WorkspaceError("mode_invalid")
    root = Path(backup_root)
    try:
        work = pathid.canonical(_absolute_folder(_expand_folder(folder)).resolve(strict=True))
    except (OSError, RuntimeError):
        raise WorkspaceError("readable_unavailable") from None
    # A folder that holds repos is a folder of repos in either mode: direct
    # mode then applies to each repo in it (a session works in that repo
    # itself), never to the folder as a whole.
    repos = not worktrees.is_repo(work) and bool(worktrees.child_repos(work))
    problem = work_folder_problem(root, work, mode, repos)
    if problem is not None:
        raise WorkspaceError(_work_problem_code(problem))
    if label is None and valid_account_label(work.name):
        label = work.name
    label = _valid_workspace_label(label)
    _migrate_legacy_before_worker_state_change(root)
    with lifecycle_lock(root):
        current = load_worker_settings(root)
        # The saved folders first: a folder of repos is expanded into its
        # repos for launching, so only the saved list still holds the parent.
        existing = work_workspace(current.workspaces, work) or work_workspace(
            launchable_workspaces(root, current.workspaces), work)
        if existing is not None:
            return WorkFolder(existing, added=False, repos=_repo_ids(root, current.workspaces, existing))
        kept, kept_builtin = current.workspaces, False
        if is_builtin_default_registry(root, kept):
            if _workspace_in_use(root, kept[0].workspace_id):
                kept_builtin = True
            else:
                kept = ()
        results = default_research_folder()
        try:
            results.mkdir(mode=0o700, exist_ok=True)
        except OSError:
            raise WorkspaceError("folder_unavailable") from None
        legacy = readable_workspace(kept, work)
        if legacy is not None:
            # A folder added as read-only before: same ID, now a work folder.
            workspace = _approve_locked(root, kept, legacy.workspace_id, legacy.output_root, (), label,
                                        work_root=work, mode=mode, repos=repos, replacing=legacy.workspace_id)
        else:
            taken = {w.workspace_id for w in kept} | set(repo_ids(root).values())
            workspace_id = suggested_folder_id(work, taken)
            workspace = _approve_locked(root, kept, workspace_id, results / workspace_id, (), label,
                                        work_root=work, mode=mode, repos=repos)
        saved = load_worker_settings(root).workspaces
        return WorkFolder(workspace, added=True, kept_builtin=kept_builtin,
                          repos=_repo_ids(root, saved, workspace))


def _repo_ids(root: Path, workspaces, parent) -> tuple[str, ...]:
    if not parent.repos:
        return ()
    static = {w.workspace_id for w in workspaces}
    return tuple(w.workspace_id for w in launchable_workspaces(root, workspaces)
                 if w.workspace_id not in static and w.work_root is not None
                 and pathid.inside(w.work_root, parent.work_root))


def set_workspace_mode(backup_root: Path, workspace_id: str, mode: str) -> WorkerWorkspace:
    """Set where a work folder's sessions run: ``worktree`` (a copy per task) or ``direct``.

    Only on this Mac: the control service can never set it. A repo found in
    a folder of repos takes its mode from that folder.
    """
    from dataclasses import replace

    from openswap.settings import WORK_MODES

    if mode not in WORK_MODES:
        raise WorkspaceError("mode_invalid")
    root = Path(backup_root)
    _migrate_legacy_before_worker_state_change(root)
    with lifecycle_lock(root):
        current = load_worker_settings(root)
        target = next((w for w in current.workspaces if w.workspace_id == workspace_id), None)
        if target is None:
            launchable = launchable_workspaces(root, current.workspaces)
            if any(w.workspace_id == workspace_id for w in launchable):
                raise WorkspaceError("mode_on_parent")
            raise WorkspaceError("workspace_not_found")
        if target.work_root is None:
            raise WorkspaceError("mode_not_work")
        if target.mode == mode:
            return target
        if _folder_in_use(root, current.workspaces, target):
            raise WorkspaceError("workspace_in_use")
        problem = work_folder_problem(root, target.work_root, mode, target.repos)
        if problem is not None:
            raise WorkspaceError(_work_problem_code(problem))
        updated = replace(target, mode=mode)
        try:
            set_worker_workspaces(root, tuple(updated if w.workspace_id == workspace_id else w
                                              for w in current.workspaces))
        except (OSError, RuntimeError, ValueError):
            raise WorkspaceError("settings_unavailable") from None
        return updated


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
        target = next((item for item in current.workspaces if item.workspace_id == workspace_id), None)
        remaining = tuple(item for item in current.workspaces if item.workspace_id != workspace_id)
        if target is None:
            raise WorkspaceError("workspace_not_found")
        if not remaining:
            raise WorkspaceError("last_workspace")
        if _folder_in_use(root, current.workspaces, target):
            raise WorkspaceError("workspace_in_use")
        try:
            set_worker_workspaces(root, remaining)
        except (OSError, RuntimeError, ValueError):
            raise WorkspaceError("settings_unavailable") from None


def _folder_ids(root: Path, workspaces, workspace) -> tuple[str, ...]:
    """Every ID a job may use for a saved folder: its own and, for a folder of repos, each repo's.

    A repo's ID is the one it was ever given (``repo_ids``), whether or not
    the repo is still offered, plus any offered now.
    """
    ids = [workspace.workspace_id]
    if getattr(workspace, "repos", False) and workspace.work_root is not None:
        for path, repo_id in repo_ids(root).items():
            if repo_id not in ids and pathid.inside(Path(path), workspace.work_root):
                ids.append(repo_id)
        ids += [repo_id for repo_id in _repo_ids(root, workspaces, workspace) if repo_id not in ids]
    return tuple(ids)


def _folder_in_use(root: Path, workspaces, workspace) -> bool:
    """Whether a job using a saved folder, or any repo in a folder of repos, may still run or upload."""
    return any(_workspace_in_use(root, workspace_id) for workspace_id in _folder_ids(root, workspaces, workspace))


def _workspace_in_use(root: Path, workspace_id: str) -> bool:
    """Whether a job using ``workspace_id`` may still run or upload its results.

    Artifact upload re-reads the registry for the job's folder, so a mapping
    stays while any job using it may still run or upload. The runtime resolves
    a job's folder under the same lifecycle lock while the job is STARTING, so
    a job either counts as in use here or resolves the changed registry. Call
    under the lifecycle lock.
    """
    try:
        unsynced, unadmitted = _unsynced_remote_work(root)
        return workspace_id in unadmitted or LocalJobStore(root).workspace_in_use(workspace_id, unsynced)
    except (OSError, sqlite3.Error, ValueError):
        raise WorkspaceError("settings_unavailable") from None


def label_worker_workspace(backup_root: Path, workspace_id: str, label: str | None) -> WorkerWorkspace:
    """Set (or, with None, reset to the folder's name) the label the control service sees."""
    root = Path(backup_root)
    label = _valid_workspace_label(label)
    _migrate_legacy_before_worker_state_change(root)
    with lifecycle_lock(root):
        current = load_worker_settings(root)
        target = next((item for item in current.workspaces if item.workspace_id == workspace_id), None)
        if target is None:
            raise WorkspaceError("workspace_not_found")
        from dataclasses import replace

        updated = replace(target, label=label)
        try:
            set_worker_workspaces(root, tuple(
                updated if item.workspace_id == workspace_id else item for item in current.workspaces
            ))
        except (OSError, RuntimeError, ValueError):
            raise WorkspaceError("settings_unavailable") from None
        return updated


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
                print(f"Remote tasks are off. {_ENABLE_NEXT}")
                return 0
    except ClaudeSwitchError:
        print(BUSY_MESSAGE, file=sys.stderr)
        return 1
    # Imported only in the dedicated worker command, after the default-off
    # policy check. The runtime owns its own SQLite journal and local socket.
    from openswap.worker.runtime import WORKER_REFUSED_MANAGED, run_worker

    result = int(run_worker(backup_root, managed=managed, service_loaded=_managed_worker_loaded))
    if result == WORKER_REFUSED_MANAGED:
        print("The worker is already running in the background; `openswap worker disable` stops it first.",
              file=sys.stderr)
        return 1
    return result


# One line each; the same wording wherever the situation comes up.
BUSY_MESSAGE = "Another worker change is in progress; try again in a moment."
SETTINGS_MESSAGE = "Could not save the worker settings."
# Plain text (not ``printer.next_step``): module constants must not latch the colour detection.
_ENABLE_NEXT = "Next: `openswap worker enable` to start the worker."


_STOP_MESSAGES = {
    "job_not_found": "no task has that ID",
    "active_job_mismatch": "a different task is running (omit the ID to stop it)",
    "stale_job_state": "the task changed state just now; try again",
}
# Why `disable` stopped short: (what is wrong, the command that clears it or None).
_DISABLE_BLOCKED = {
    "job_still_active": ("a task is still running", "`openswap worker stop` to end it."),
    "job_stop_not_confirmed": ("the running task has not stopped yet", "wait, then `openswap worker disable` again."),
    "lease_state_unknown": ("an account may still be in use", None),  # step: _lease_release_step
    "worker_state_unknown": ("the worker's state could not be read", None),
    "worker_status_unavailable": ("the worker's state could not be read", None),
    "worker_unload_failed": ("the background service could not be unloaded", None),
    "settings_unavailable": ("the worker settings could not be saved", None),
    "job_identity_unknown": ("the running task could not be identified", None),
}
_LEASE_MESSAGES = {
    "lease_not_found": "no account is held.",
    "lease_state_unknown": "the account's state could not be read.",
    "worker_owner_may_be_alive": "the worker holding it may still be running.",
    "lease_not_expired": "it is still within its time; wait for it to expire.",
    "job_not_terminal": "its task has not ended.",
    "provider_start_pending": "its task is still starting; wait for the worker to record it.",
    "stop_unproven_confirm_required": ("it cannot prove the task stopped. Once nothing runs on the account, "
                                       "pass --confirm-stopped."),
}


def _lease_release_step(root: Path) -> str:
    """The `lease release` command for each account store still held (Claude needs `--provider claude`).

    Read-only: the same snapshot the status reads. When neither store can be
    read, both commands are named rather than guessing the Codex default.
    """
    held = []
    for provider in ("codex", "claude"):
        try:
            lease = AccountLeaseStore(root, provider).read_current()
        except Exception:
            held = []
            break
        if lease is not None and lease.state in {"active", "uncertain"}:
            held.append(provider)
    if not held:
        held = ["codex", "claude"]
    commands = [f"`openswap worker lease release{' --provider claude' if p == 'claude' else ''}`" for p in held]
    return f"{' or '.join(commands)} to free it."


def _migration_message(exc: ClaudeSwitchError) -> str:
    """One line when the legacy backup folder could not be moved first."""
    return f"Could not move the old OpenSwap data folder: {exc}"


_WORKER_COMMANDS = (
    "setup", "pair", "unpair", "status", "enable", "disable", "pause", "stop", "account", "workspace",
    "worktrees", "codex", "claude", "live", "live-check", "lease", "run", "submit-test", "refserver",
)


def unknown_command_message(prog: str, word: str, commands) -> str:
    """``unknown command`` plus a "did you mean" for a close mistype, else the help hint."""
    import difflib

    close = difflib.get_close_matches(word, list(commands), n=3, cutoff=0.6)
    if close:
        suggestion = " or ".join(f"`{prog} {name}`" for name in close)
        return f"{prog}: unknown command '{word}'. Did you mean {suggestion}?"
    return f"{prog}: unknown command '{word}'. Run `{prog} --help` to list the commands."


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
    if arguments and not arguments[0].startswith("-") and arguments[0] not in _WORKER_COMMANDS:
        print(unknown_command_message("openswap worker", arguments[0], _WORKER_COMMANDS), file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(
        prog="openswap worker",
        description="Remote tasks on this Mac: the worker, the account, the folders and the live check.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("refserver", help="serve the reference protocol or manage pairing/revocation")
    commands.add_parser("codex", help="install and sign in the Codex CLI tasks run with")
    commands.add_parser("live", help="show, turn on or turn off live tasks")
    commands.add_parser("claude", help="pin Claude Code and sign the account in for tasks")
    commands.add_parser("live-check", help="run a few short real tasks here and record the evidence")
    run = commands.add_parser("run", help="run the background worker process")
    # Passed only by the LaunchAgent: a manual run refuses while it is loaded.
    run.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)
    pair_parser = commands.add_parser("pair", help="pair this Mac with the pairing command from OpenTag")
    pair_parser.add_argument("url")
    pair_parser.add_argument("code")
    setup_parser = commands.add_parser(
        "setup", help="guided setup: the worker, the account and the folders tasks work in",
        description="The steps `pair` runs after pairing: start the worker, pick the account, pick the "
                    "folders tasks work in (a git repo, or a folder of repos such as ~/GitHub), then a "
                    "checklist with one next step. Each task works in its own git worktree, so your copy "
                    "is never touched; results go to ~/OpenSwap Research. Only each folder's ID and name "
                    "leave this Mac, never its path.",
    )
    setup_parser.add_argument(
        "--advanced", action="store_true",
        help="also offer to run tasks in a folder itself instead of a worktree",
    )
    unpair_parser = commands.add_parser(
        "unpair", help="unpair this Mac (pass a URL to forget an old pairing instead)",
    )
    unpair_parser.add_argument("url", nargs="?", default=None, help="the service to forget (default: the paired one)")
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
    status_parser = commands.add_parser("status", help="the worker, the service link and the running task")
    status_parser.add_argument("--json", action="store_true")
    stop_parser = commands.add_parser("stop", help="stop the running task")
    stop_parser.add_argument("job_id", nargs="?", help="omit to target the running task")
    stop_parser.add_argument("--json", action="store_true")
    pause_parser = commands.add_parser("pause", help="stop taking new tasks (--off takes them again)")
    pause_parser.add_argument("--off", action="store_true", help="take new tasks again")
    pause_parser.add_argument("--json", action="store_true")
    enable_parser = commands.add_parser("enable", help="start the worker (it stays on after a restart)")
    enable_parser.add_argument("--json", action="store_true")
    disable_parser = commands.add_parser("disable", help="stop the worker and turn Remote tasks off")
    disable_parser.add_argument("--json", action="store_true")
    lease_parser = commands.add_parser("lease", help="free an account a crashed task still holds")
    lease_commands = lease_parser.add_subparsers(dest="lease_command", required=True)
    lease_release_parser = lease_commands.add_parser(
        "release", help="free the account once its task is proven gone"
    )
    lease_release_parser.add_argument("--provider", choices=("codex", "claude"), default="codex")
    lease_release_parser.add_argument(
        "--confirm-stopped", action="store_true",
        help="confirm that nothing is still running on the account, when that cannot be proven",
    )
    lease_release_parser.add_argument("--json", action="store_true")
    account_parser = commands.add_parser(
        "account",
        help="list the accounts, or pin the one tasks run on",
        description="With no argument, list the Codex and Claude accounts and the pinned one. Pass a "
                    "slot, email or alias to pin that account (`claude:4` or `codex:2` names the kind; a "
                    "bare slot is Codex first). `allow`, `disallow` and `label` manage the accounts a "
                    "task may pick instead (see `account allow --help`).",
    )
    account_parser.add_argument("selector", nargs="?", metavar="SLOT|EMAIL|ALIAS")
    account_parser.add_argument("--clear", action="store_true", help="remove the pin")
    account_parser.add_argument("--json", action="store_true")
    workspace_parser = commands.add_parser(
        "workspace", help="the folders tasks work in or read",
    )
    workspace_commands = workspace_parser.add_subparsers(dest="workspace_command", required=True)
    workspace_list = workspace_commands.add_parser("list", help="list the folders")
    workspace_list.add_argument("--json", action="store_true")
    workspace_add = workspace_commands.add_parser(
        "add", help="add a folder tasks work in (--work DIR) or read (--read DIR)",
        usage="%(prog)s --work DIR [--direct] [--label TEXT] [--json]\n"
              "       %(prog)s --read DIR [--label TEXT] [--json]\n"
              "       %(prog)s ID FOLDER [--readonly-source DIR] [--label TEXT] [--json]",
        description="`--work DIR` lets tasks work in DIR: a git repo becomes one folder (ID from its "
                    "name); a folder of repos offers each repo in it. Each task works in its own git "
                    "worktree on a branch openswap/<task>, so your copy is never touched; `--direct` "
                    "works in DIR itself. `--read DIR` lets tasks read DIR but never change it. "
                    "`ID FOLDER` makes FOLDER a results folder. Results go to ~/OpenSwap Research/<ID>.",
    )
    workspace_add.add_argument("workspace_id", metavar="ID", nargs="?")
    workspace_add.add_argument("folder", metavar="FOLDER", nargs="?")
    workspace_add.add_argument(
        "--work", metavar="DIR", default=None,
        help="a git repo (or a folder of repos) tasks work in, each in its own worktree",
    )
    workspace_add.add_argument(
        "--direct", action="store_true",
        help="with --work: tasks work in DIR itself, not in a worktree (any folder)",
    )
    workspace_add.add_argument(
        "--read", metavar="DIR", default=None,
        help="a folder tasks may read but never change",
    )
    workspace_add.add_argument(
        "--readonly-source", action="append", metavar="DIR",
        help="with ID FOLDER: a folder the task may read but never write (repeatable)",
    )
    workspace_add.add_argument(
        "--label", default=None,
        help="the name shown for this folder (1-100 characters; default: the folder's name)",
    )
    workspace_add.add_argument("--json", action="store_true")
    workspace_label = workspace_commands.add_parser(
        "label", help="rename the name shown for a folder",
    )
    workspace_label.add_argument("workspace_id", metavar="ID")
    workspace_label.add_argument("label", metavar="TEXT", nargs="?", default=None)
    workspace_label.add_argument("--reset", action="store_true", help="show the folder's own name again")
    workspace_label.add_argument("--json", action="store_true")
    workspace_remove = workspace_commands.add_parser("remove", help="remove a folder (its files are kept)")
    workspace_remove.add_argument("workspace_id", metavar="ID")
    workspace_remove.add_argument("--json", action="store_true")
    workspace_mode = workspace_commands.add_parser(
        "mode", help="where a folder's tasks work: worktree (a copy per task) or direct",
        description="`worktree` (the default) gives each task its own git worktree; `direct` runs the "
                    "task in the folder itself, like running `claude` or `codex` there. Set only here, "
                    "never remotely.",
    )
    workspace_mode.add_argument("workspace_id", metavar="ID")
    workspace_mode.add_argument("mode", choices=("worktree", "direct"))
    workspace_mode.add_argument("--json", action="store_true")
    worktrees_parser = commands.add_parser(
        "worktrees", help="list the tasks' worktrees, or prune finished ones",
        description="Each task works in its own git worktree under ~/OpenSwap Research/.worktrees. "
                    "A finished task's branch is always kept; `prune` removes its worktree when clean "
                    "(with --force, even with uncommitted work).",
    )
    worktrees_parser.add_argument("action", nargs="?", choices=("list", "prune"), default="list")
    worktrees_parser.add_argument("--force", action="store_true",
                                  help="with prune: also remove worktrees with uncommitted work or a lock")
    worktrees_parser.add_argument("--json", action="store_true")
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
            print(_migration_message(exc), file=sys.stderr)
            return 1
        try:
            if args.command == "pair":
                worker_id = pair(root, args.url, args.code)
                print(f"{printer.MARK_OK} Paired this Mac ({worker_id}).")
                # Pairing already succeeded; the guided steps are optional and
                # print the manual command for any step that fails.
                try:
                    _guided_setup(root)
                except Exception:
                    print("Next: `openswap worker setup` to finish setting up.")
            else:
                if unpair(root, args.url):
                    print(f"{printer.MARK_OK} Unpaired. Remote tasks are off; remote access disabled.")
                elif load_worker_settings(root).control_service_url is not None:
                    # Only an orphan was removed: the configured enrollment is still live.
                    print(f"{printer.MARK_OK} Forgot that old pairing. The current pairing is unchanged; "
                          "`openswap worker unpair` without a URL ends it.")
                else:
                    print(f"{printer.MARK_OK} Forgot that pairing. This Mac is not paired.")
            return 0
        except ProtocolError as exc:
            print(f"Could not {args.command} ({exc.code}).", file=sys.stderr)
            return 1
        except (OSError, RuntimeError, ValueError):
            print(f"Could not {args.command}: the worker settings are unavailable.", file=sys.stderr)
            return 1

    if args.command in {"run", "enable", "disable", "pause"}:
        try:
            _migrate_legacy_before_worker_state_change(root)
        except ClaudeSwitchError as exc:
            print(_migration_message(exc), file=sys.stderr)
            return 1

    if args.command == "setup":
        try:
            _migrate_legacy_before_worker_state_change(root)
            paired = load_worker_settings(root).control_service_url is not None
        except ClaudeSwitchError as exc:
            print(_migration_message(exc), file=sys.stderr)
            return 1
        except (OSError, RuntimeError, ValueError):
            print("Could not read the worker settings.", file=sys.stderr)
            return 1
        if not paired:
            print("Not paired yet. " + printer.next_step(
                "run the pairing command from OpenTag's Workers page: `openswap worker pair <url> <code>`."),
                file=sys.stderr)
            return 1
        _guided_setup(root, advanced=args.advanced)
        return 0
    if args.command == "run":
        return _run(root, managed=args.managed)
    if args.command == "account":
        return _account_command(root, args)
    if args.command == "workspace":
        return _workspace_command(root, args)
    if args.command == "worktrees":
        return _worktrees_command(root, args)
    if args.command == "status":
        try:
            snapshot = read_status(root)
        except Exception:
            print("Could not read the worker status.", file=sys.stderr)
            return 1
        try:
            refused = refused_workspaces(root)
        except Exception:
            refused = []
        if refused:
            snapshot = {**snapshot, "refused_workspaces": [
                {"workspace_id": workspace_id, "diagnostic_code": code} for workspace_id, code in refused]}
        human = None
        if not args.json:
            human = _format_status(snapshot)
            for workspace_id, code in refused:
                human = f"{human}\n{refusal_line(workspace_id, code)}"
            if refused:
                human = f"{human}\n" + printer.next_step(
                    "`openswap worker workspace remove <id>` to drop a blocked folder "
                    "(`openswap worker workspace list` shows them).")
            hint = _worker_off_hint(root, snapshot)
            if hint is not None:
                human = f"{human}\n{hint}"
        _write(snapshot, as_json=args.json, human=human)
        return 0
    if args.command == "stop":
        try:
            result = request_stop(root, args.job_id)
        except IpcError as exc:
            print(f"Could not reach the worker ({exc}).", file=sys.stderr)
            return 1
        code = result.get("diagnostic_code")
        if result.get("accepted") is not True:
            _write(result, as_json=args.json,
                   human=f"{printer.MARK_BAD} Not stopped: {_STOP_MESSAGES.get(code, 'the worker refused')} ({code}).")
            return 1
        human = (f"{printer.MARK_DOT} No task is running." if code in {"no_active_job", "job_not_active"}
                 else f"{printer.MARK_OK} Stopping the task. `openswap worker status` shows when it has ended.")
        _write(result, as_json=args.json, human=human)
        return 0
    if args.command == "pause":
        paused = not args.off
        try:
            payload = request_pause(root, paused)
        except (IpcError, ClaudeSwitchError):
            print("Could not change whether the worker takes tasks.", file=sys.stderr)
            return 1
        except (OSError, RuntimeError, ValueError):
            print(SETTINGS_MESSAGE, file=sys.stderr)
            return 1
        _write(
            payload,
            as_json=args.json,
            human=(f"{printer.MARK_OK} Paused: no new task starts. `openswap worker pause --off` resumes."
                   if paused else f"{printer.MARK_OK} Taking tasks again."),
        )
        return 0 if payload["accepted"] else 1
    if args.command == "enable":
        try:
            payload = enable_worker(root)
        except ClaudeSwitchError as exc:
            print(_enable_failure_message(exc), file=sys.stderr)
            return 1
        from openswap.worker.guided_setup import WORKER_ONLINE

        _write(payload, as_json=args.json, human=WORKER_ONLINE)
        return 0
    if args.command == "disable":
        try:
            ok, result, diagnostic = disable_worker(root)
        except ClaudeSwitchError:
            print(BUSY_MESSAGE, file=sys.stderr)
            return 1
        if not ok:
            if diagnostic == "worker_stop_unconfirmed":
                print("The worker is still stopping; wait a moment before `openswap worker enable`.",
                      file=sys.stderr)
            else:
                # A blocked disable restores the prior opt-in, so report the
                # persisted policy rather than assuming it is still enabled.
                try:
                    state = "on" if load_worker_settings(root).enabled else "off"
                except Exception:
                    state = None
                reason, step = _DISABLE_BLOCKED.get(diagnostic, ("the worker could not be proven idle", None))
                if diagnostic == "lease_state_unknown":
                    step = _lease_release_step(root)
                where = "no new task starts" if state is None else f"Remote tasks stay {state}, paused"
                line = f"Not stopped: {reason} ({diagnostic}). {where}."
                print(line if step is None else f"{line} {printer.next_step(step)}", file=sys.stderr)
            return 1
        payload = {"enabled": False, **result}
        _write(payload, as_json=args.json, human=f"{printer.MARK_OK} Worker stopped. Remote tasks are off.")
        return 0
    if args.command == "lease" and args.lease_command == "release":
        try:
            ok, result, diagnostic = release_lease(
                root, args.provider, confirm_stopped=args.confirm_stopped
            )
        except ClaudeSwitchError:
            print(BUSY_MESSAGE, file=sys.stderr)
            return 1
        if not ok:
            print(f"Not released ({diagnostic}): {_LEASE_MESSAGES.get(diagnostic, 'the account may still be in use.')}",
                  file=sys.stderr)
            return 1
        payload = {"released": True, **result}
        human = (f"{printer.MARK_DOT} The account was already free." if diagnostic == "already_released"
                 else f"{printer.MARK_OK} Account freed.")
        _write(payload, as_json=args.json, human=human)
        return 0
    parser.error("unsupported worker command")
    return 2


# Errors are one line: what is wrong, then what to do.
_ACCOUNT_MESSAGES = {
    "claude_not_supported": "That is a Claude account: name it `claude:<slot>`.",
    "account_not_found": "No account matches that. `openswap worker account` lists them.",
    "account_ambiguous": "That matches several accounts; use the slot number (`claude:<slot>` for Claude).",
    "account_not_eligible": "That account can't be pinned: a Codex API-key login, or a Claude slot without an email.",
    "roster_unavailable": "Could not read the accounts.",
    "account_roster_busy": "The accounts are being changed; try again in a moment.",
    "worker_lifecycle_busy": BUSY_MESSAGE,
    "settings_unavailable": SETTINGS_MESSAGE,
    "too_many_accounts": "At most 20 accounts can be allowed; disallow one first.",
    "account_not_allowlisted": "Tasks can't pick that account yet. `openswap worker account` lists the allowed ones.",
    "account_is_default": "That is the pinned account. Pin another first, or pass --clear-default.",
    "label_invalid": "A label is 1-100 characters with no control characters.",
}

_WORKSPACE_MESSAGES = {
    "workspace_id_invalid": "An ID is 1-64 characters: lowercase letters, digits, '-' and '_', starting with a letter or digit.",
    "workspace_exists": "That ID is taken. Remove it first to change its folder.",
    "workspace_not_found": "No folder has that ID. `openswap worker workspace list` shows them.",
    "last_workspace": "The last folder can't be removed; add another first.",
    "workspace_in_use": "A task is still running or uploading in that folder; try again when it has ended.",
    "too_many_workspaces": "At most 16 folders can be added.",
    "too_many_readonly_sources": "At most 16 read-only folders per results folder.",
    "folder_invalid": "That is not a valid path.",
    "folder_unavailable": "That folder could not be created or read.",
    "folder_unsafe": "That is not a real folder (a symlink or a file).",
    "folder_permissions": "Only you may open a results folder: run `chmod 700` on it, then try again.",
    "folder_exposes_credentials": "That folder is, or holds, your home folder or a sign-in folder. Pick a dedicated one.",
    "readonly_source_exposes_credentials": "That folder overlaps your home folder or a sign-in folder.",
    "readonly_source_unavailable": "That read-only folder doesn't exist or can't be opened.",
    "readonly_source_unsafe": "That read-only folder is not a real folder (a symlink or a file).",
    "readonly_source_not_owned": "That read-only folder isn't yours. Pick one you own.",
    "readonly_source_permissions": "Other users can change that read-only folder: run `chmod go-w` on it.",
    "readonly_source_overlaps_folder": "A results folder and its read-only folders must not overlap.",
    "readable_system": "That's a system folder. Pick one with your own files, like ~/GitHub.",
    "readable_home": "That's your whole home folder. Pick a folder inside it, like ~/GitHub.",
    "readable_private": "That's app data or a hidden folder. Pick one with your own files.",
    "readable_exposes_credentials": "That folder holds OpenSwap, Codex or Claude sign-ins. Pick another.",
    "readable_results": "That's where results are saved. Pick a folder with your own files.",
    "readable_unavailable": "That folder doesn't exist or can't be opened.",
    "readable_unsafe": "That's not a folder.",
    "readable_not_owned": "That folder isn't yours. Pick one you own.",
    "readonly_source_overlaps_results": "That folder holds another folder's task results. Pick another.",
    "folder_overlaps_readable": "That results folder is inside a folder tasks use. Pick another place.",
    "readable_permissions": "Other users can change that folder: run `chmod go-w` on it, then try again.",
    "work_not_a_repo": "That's not a git repo and holds none. Pick a repo or a folder of repos (`--direct` takes any folder).",
    "work_no_repos": "That folder holds no git repos anymore. Pick a repo instead.",
    "work_overlaps_results": "That folder overlaps where task results are saved. Pick another.",
    "folder_overlaps_work": "That results folder is inside a folder tasks work in. Pick another place.",
    "work_overlaps_readable": "That folder overlaps one tasks only read. Remove that one first (`openswap worker workspace remove <id>`).",
    "mode_invalid": "The mode is `worktree` or `direct`.",
    "mode_not_work": "Only a folder tasks work in has a mode (add one with `--work`).",
    "mode_on_parent": "That repo comes from a folder of repos: set the mode on that folder's ID.",
    "worktree_failed": "Could not make the task's worktree of the repo.",
    "label_invalid": "A label is 1-100 characters with no control characters.",
    "worker_lifecycle_busy": BUSY_MESSAGE,
    "settings_unavailable": SETTINGS_MESSAGE,
}


def _account_rows(choices: AccountChoices, accounts) -> list[tuple[str, ...]]:
    rows = []
    for choice in accounts:
        pinned = choice.account_ref is not None and choice.account_ref == choices.pinned_ref
        notes = []
        if pinned:
            notes.append("pinned")
        if not choice.eligible:
            notes.append("API key: can't be pinned" if choice.provider == "codex" else "no email: can't be pinned")
        if choice.disabled:
            notes.append("out of rotation")
        rows.append((f"{printer.mark(True if pinned else False if not choice.eligible else None)} "
                     f"{choice.number}", choice.email or "(no email)",
                     f"({choice.alias})" if choice.alias else "", ", ".join(notes)))
    return rows


def _format_accounts(choices: AccountChoices, next_step: str | None = None) -> str:
    """The human ``openswap worker account`` listing: both kinds, the allowed ones, one next step.

    ``next_step`` is what follows once an account is pinned (the pinned
    kind's live-check path); ``None`` when nothing is left to do.
    """
    lines = [printer.heading("Remote tasks account"),
             printer.heading("Codex (pin by slot, email or alias)")]
    if not choices.codex:
        lines.append("  none (`openswap codex add` saves one)")
    lines.extend(printer.columns(_account_rows(choices, choices.codex)))
    lines.append(printer.heading("Claude (pin with claude:<slot>)"))
    if not choices.claude:
        lines.append("  none (`openswap add` saves one)")
    lines.extend(printer.columns(_account_rows(choices, choices.claude)))
    if choices.allowlist:
        # Shown to the service by label only; a task may pick one instead of the pin.
        lines.append(printer.heading("Also allowed for tasks to pick"))
        lines.extend(printer.columns([_allowlist_row(entry, choices) for entry in choices.allowlist]))
    if choices.pinned_missing:
        lines.append(f"{printer.MARK_BAD} The pinned account is gone; tasks fail until you pin another.")
        lines.append(printer.next_step(f"`openswap worker account {_example(choices)}` to pin an account."))
    elif choices.pinned_ref is None:
        lines.append(printer.next_step(f"`openswap worker account {_example(choices)}` to pin the account "
                                       "tasks run on."))
    elif next_step is not None:
        lines.append(printer.next_step(next_step))
    return "\n".join(lines)


def _example(choices: AccountChoices) -> str:
    """A selector for the first eligible account, so the next step is a command that works."""
    for choice in choices.codex:
        if choice.eligible:
            return choice.number
    for choice in choices.claude:
        if choice.eligible:
            return f"claude:{choice.number}"
    return "<slot|email|alias>"


def _allowlist_row(entry: AllowlistedAccount, choices: AccountChoices) -> tuple[str, ...]:
    """``mark  Kind slot  "label"  note``; a gone account shows the reference ``disallow`` takes."""
    default = entry.identity == choices.pinned_ref
    slot = choices.slot_for(entry.identity)
    provider = "Claude" if entry.identity.startswith("claude:") else "Codex"
    where = f"{provider} {slot.number}" if slot is not None else f"{provider} (removed)"
    note = "pinned" if default else "" if slot is not None else f"gone: disallow {entry.account_ref}"
    return (f"{printer.mark(True if default else None if slot is not None else False)} {where}",
            json.dumps(entry.label, ensure_ascii=False), note)


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
        description="The accounts a task may pick instead of the pinned one. Each is shown to the "
                    "service only by a label you choose (by default the alias, or 'Codex account N' / "
                    "'Claude account N'), never the email.",
    )
    commands = parser.add_subparsers(dest="allowlist_command", required=True)
    allow = commands.add_parser("allow", help="let tasks pick this account")
    allow.add_argument("selector", metavar="SLOT|EMAIL|ALIAS")
    allow.add_argument("--label", default=None, help="the name shown for it (1-100 characters)")
    allow.add_argument("--json", action="store_true")
    disallow = commands.add_parser("disallow", help="stop tasks picking this account")
    disallow.add_argument("target", metavar="SLOT|EMAIL|ALIAS|REF")
    disallow.add_argument("--clear-default", action="store_true",
                          help="also remove the pin when it is the pinned account")
    disallow.add_argument("--json", action="store_true")
    label = commands.add_parser("label", help="rename the name shown for an allowed account")
    label.add_argument("target", metavar="SLOT|EMAIL|ALIAS|REF")
    label.add_argument("label", metavar="TEXT")
    label.add_argument("--json", action="store_true")
    args = parser.parse_args(arguments)
    try:
        if args.allowlist_command == "allow":
            entry = allow_worker_account(root, args.selector, args.label)
            human = f"{printer.MARK_OK} Tasks may pick {_allowed_name(root, entry)}."
        elif args.allowlist_command == "disallow":
            entry = disallow_worker_account(root, args.target, clear_default=args.clear_default)
            human = f"{printer.MARK_OK} Tasks can no longer pick {_allowed_name(root, entry)}."
        else:
            entry = label_worker_account(root, args.target, args.label)
            human = f"{printer.MARK_OK} Renamed: {_allowed_name(root, entry)}."
        pinned = load_worker_settings(root).pinned_account_ref
    except AccountPinError as exc:
        code = exc.code
    except ClaudeSwitchError as exc:
        code = str(exc) if str(exc) == "worker_lifecycle_busy" else "settings_unavailable"
    else:
        if args.allowlist_command == "disallow" and args.clear_default and pinned is None:
            human += " " + printer.next_step("`openswap worker account <slot>` to pin an account.")
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
    """``mark  ID  "label"  what tasks do there``; the ID is what every other command takes."""
    lines = [printer.heading("Folders")]
    rows = []
    for workspace in workspaces:
        work = getattr(workspace, "work_root", None)
        if work is not None:
            how = "works in the folder itself" if workspace.mode == "direct" else "own worktree per task"
            where = f"works in each repo in {display_path(work)}" if workspace.repos else f"works in {display_path(work)}"
            use = f"{where} ({how})"
        else:
            # A results folder, with the folders its tasks may read first (a `--read` folder,
            # or `ID FOLDER --readonly-source DIR`): both places are the owner's to find.
            use = f"writes results in {display_path(workspace.output_root)}"
            if workspace.readonly_roots:
                use = "reads " + ", ".join(display_path(source) for source in workspace.readonly_roots) + f"; {use}"
        rows.append((f"{printer.MARK_OK} {workspace.workspace_id}",
                     json.dumps(workspace.display_label, ensure_ascii=False), use))
    lines.extend(printer.columns(rows))
    lines.append(printer.next_step("`openswap worker workspace add --work <folder>` adds one; "
                                   "`openswap worker workspace remove <id>` drops one."))
    return "\n".join(lines)


def _named(workspace) -> str:
    """``"id"``, plus the label when the owner chose one."""
    name = f'"{workspace.workspace_id}"'
    if getattr(workspace, "label", None) is not None:
        name += f" (shown as {json.dumps(workspace.display_label, ensure_ascii=False)})"
    return name


def _allowed_name(root: Path, entry: AllowlistedAccount) -> str:
    """``"label" (Codex 1)`` for an allowed account; the reference when its slot is gone."""
    try:
        slot = worker_account_choices(root).slot_for(entry.identity)
    except Exception:
        slot = None
    provider = "Claude" if entry.identity.startswith("claude:") else "Codex"
    where = f"{provider} {slot.number}" if slot is not None else entry.account_ref
    return f"{json.dumps(entry.label, ensure_ascii=False)} ({where})"


def _workspace_payload(workspace) -> dict:
    payload = {
        "workspace_id": workspace.workspace_id,
        "label": workspace.display_label,
        "output_root": str(workspace.output_root),
        "readonly_roots": [str(root) for root in workspace.readonly_roots],
    }
    if workspace.work_root is not None:
        payload.update(work_root=str(workspace.work_root), mode=workspace.mode, repos=workspace.repos)
    return payload


def _worktrees_command(root: Path, args) -> int:
    from openswap.worker import worktrees

    results = default_research_folder()
    store = LocalJobStore(root)

    def finished(job_id: str) -> bool:
        try:
            return store.get(job_id).state.value in {"succeeded", "failed", "cancelled", "interrupted", "expired"}
        except Exception:
            return True

    removed = []
    if args.action == "prune":
        try:
            removed = worktrees.sweep(results, finished, force=args.force)
        except Exception:
            print("Could not prune the worktrees.", file=sys.stderr)
            return 1
    items = worktrees.list_all(results)
    payload = {
        "worktrees": [{
            "workspace_id": item.workspace_id, "job_id": item.job_id, "path": str(item.path),
            "branch": item.branch, "dirty": item.dirty, "locked": item.locked,
            "repo_present": item.available, "finished": finished(item.job_id),
        } for item in items],
        "removed": [str(item.path) for item in removed],
    }
    rows = []
    for item in items:
        done = finished(item.job_id)
        state = ("repo moved or deleted" if not item.available
                 else "uncommitted work" if item.dirty else "clean" if item.dirty is False else "unknown")
        if item.locked:
            state += ", locked"
        rows.append((f"{printer.mark(None if done else True)} {item.workspace_id}",
                     "finished" if done else "running", item.branch or "-", state, display_path(item.path)))
    lines = [printer.heading("Task worktrees")]
    if args.action == "prune":
        lines.append(f"{printer.MARK_OK} Removed {len(removed)} (branches are kept).")
    lines.extend(printer.columns(rows) or ["  none"])
    if args.action == "list" and any(finished(item.job_id) for item in items):
        lines.append(printer.next_step("`openswap worker worktrees prune` to remove the finished ones."))
    _write(payload, as_json=args.json, human="\n".join(lines))
    return 0


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


def _guided_setup(root: Path, *, interactive: bool | None = None, read_line=None, advanced: bool = False) -> None:
    """The guided steps after pairing (and `openswap worker setup`), on this terminal.

    Start the worker, confirm the account, pick the folders where sessions work, then a
    summary. Without a terminal it prints each step's command instead.
    """
    from openswap.worker import guided_setup

    if interactive is None:
        interactive = _interactive_terminal()
    ui = guided_setup.TerminalPrompts(interactive=interactive, read_line=read_line)
    ui.advanced = advanced
    guided_setup.run(root, ui)


def _account_command(root: Path, args) -> int:
    if args.clear and args.selector is not None:
        print("Pass an account or --clear, not both.", file=sys.stderr)
        return 2
    if args.selector is None and not args.clear:
        try:
            choices = worker_account_choices(root)
        except Exception:
            print("Could not read the accounts.", file=sys.stderr)
            return 1
        human = None
        if not args.json:
            pinned = choices.pinned if choices.pinned_ref is not None and not choices.pinned_missing else None
            human = _format_accounts(choices, None if pinned is None else _after_pin(root, _kind(pinned)))
        _write(choices.to_dict(), as_json=args.json, human=human)
        return 0
    try:
        choice = set_worker_account(root, None if args.clear else args.selector)
    except AccountPinError as exc:
        code = exc.code
    except ClaudeSwitchError as exc:
        code = str(exc) if str(exc) == "worker_lifecycle_busy" else "settings_unavailable"
    else:
        payload = {"accepted": True, "pinned": _choice_payload(choice)}
        if choice is None:
            human = (f"{printer.MARK_OK} Pin removed; tasks fail until you pin an account.\n"
                     + printer.next_step("`openswap worker account <slot>` to pin one."))
        else:
            human = f"{printer.MARK_OK} Pinned {_kind(choice)} {choice.label()}."
            after = _after_pin(root, _kind(choice))
            if after is not None:
                human += "\n" + printer.next_step(after)
        _write(payload, as_json=args.json, human=human)
        return 0
    if args.json:
        _write({"accepted": False, "diagnostic_code": code}, as_json=True)
    else:
        print(_ACCOUNT_MESSAGES.get(code, f"Could not pin that account ({code})."), file=sys.stderr)
    return 1


def _kind(choice) -> str:
    return "Claude" if getattr(choice, "provider", "codex") == "claude" else "Codex"


def _after_pin(root: Path, provider: str) -> str | None:
    """The step after pinning: the kind's live-check path while its live tasks are off, else nothing."""
    try:
        from openswap.worker.live import execution_mode

        live = execution_mode(root, provider.lower()) == "live"
    except Exception:
        live = False
    if live:
        return None
    if provider == "Claude":
        return ("`openswap worker claude pin`, `openswap worker claude prepare`, then "
                "`openswap worker live-check --provider claude` to turn on live tasks.")
    return "`openswap worker codex install`, then `openswap worker live-check` to turn on live tasks."


def _workspace_command(root: Path, args) -> int:
    try:
        if args.workspace_command == "list":
            workspaces = load_worker_settings(root).workspaces
            _write(
                {"workspaces": [_workspace_payload(item) for item in workspaces]},
                as_json=args.json, human=_format_workspaces(workspaces),
            )
            return 0
        if args.workspace_command == "add" and args.work is not None:
            if args.workspace_id is not None or args.folder is not None or args.readonly_source or args.read:
                print("Pass `--work DIR` on its own.", file=sys.stderr)
                return 2
            result = add_work_folder(root, args.work, mode="direct" if args.direct else "worktree",
                                     label=args.label)
            workspace = result.workspace
            where = display_path(workspace.work_root)
            how = "works in the folder itself" if workspace.mode == "direct" else "own worktree per task"
            if not result.added:
                human = f"{printer.MARK_DOT} {where} is already added as \"{workspace.workspace_id}\"."
            elif result.repos or workspace.repos:
                human = (f"{printer.MARK_OK} Added {where}: tasks can work in "
                         f"{', '.join(result.repos) or 'its repos (none yet)'} ({how}).")
            else:
                human = f"{printer.MARK_OK} Added {where} as {_named(workspace)} ({how})."
            _write({"accepted": True, "added": result.added, "workspace": _workspace_payload(workspace),
                    "repos": list(result.repos)}, as_json=args.json, human=human)
            return 0
        if args.workspace_command == "add" and args.direct:
            print("`--direct` goes with `--work DIR`.", file=sys.stderr)
            return 2
        if args.workspace_command == "mode":
            workspace = set_workspace_mode(root, args.workspace_id, args.mode)
            how = "in the folder itself" if workspace.mode == "direct" else "in a worktree of their own"
            _write({"accepted": True, "workspace": _workspace_payload(workspace)}, as_json=args.json,
                   human=f"{printer.MARK_OK} Tasks in \"{workspace.workspace_id}\" now work {how}.")
            return 0
        if args.workspace_command == "add" and args.read is not None:
            if args.workspace_id is not None or args.folder is not None or args.readonly_source:
                print("Pass either `--read DIR` or `ID FOLDER`, not both.", file=sys.stderr)
                return 2
            result = add_readable_folder(root, args.read, label=args.label)
            workspace = result.workspace
            (source,) = workspace.readonly_roots
            if result.added:
                human = (f"{printer.MARK_OK} Added {display_path(source)} as {_named(workspace)} "
                         "(tasks read it, never change it).")
            else:
                human = f"{printer.MARK_DOT} {display_path(source)} is already added as \"{workspace.workspace_id}\"."
            _write({"accepted": True, "added": result.added, "workspace": _workspace_payload(workspace)},
                   as_json=args.json, human=human)
            return 0
        if args.workspace_command == "add":
            if args.workspace_id is None or args.folder is None:
                print("Pass `--work DIR`, `--read DIR`, or `ID FOLDER`.", file=sys.stderr)
                return 2
            workspace = add_worker_workspace(
                root, args.workspace_id, args.folder, tuple(args.readonly_source or ()), label=args.label,
            )
            _write(
                {"accepted": True, "workspace": _workspace_payload(workspace)},
                as_json=args.json,
                human=f"{printer.MARK_OK} Added {display_path(workspace.output_root)} as "
                      f"{_named(workspace)} (tasks write their results there).",
            )
            return 0
        if args.workspace_command == "label":
            if (args.label is None) == (not args.reset):
                print("Pass a label or --reset, not both.", file=sys.stderr)
                return 2
            workspace = label_worker_workspace(root, args.workspace_id, None if args.reset else args.label)
            _write(
                {"accepted": True, "workspace": _workspace_payload(workspace)},
                as_json=args.json,
                human=f"{printer.MARK_OK} \"{workspace.workspace_id}\" is shown as "
                      f"{json.dumps(workspace.display_label, ensure_ascii=False)}.",
            )
            return 0
        remove_worker_workspace(root, args.workspace_id)
        _write(
            {"accepted": True, "removed": args.workspace_id}, as_json=args.json,
            human=f"{printer.MARK_OK} Removed \"{args.workspace_id}\" (its files are kept).",
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
        return "Could not start the worker: its settings are invalid (the pinned account or a folder)."
    if code == "worker_stop_unconfirmed":
        return "Could not start the worker: it is still stopping; try again in a moment."
    if code == "worker_running_unmanaged":
        return "Could not start the worker: `openswap worker run` is already running it."
    if code == "kickoff_in_progress":
        return "Could not start the worker: a scheduled kickoff is running; try again in a moment."
    if code == "worker_lifecycle_busy":
        return BUSY_MESSAGE
    return "Could not start the worker."


# enable_worker's own refusal codes. Anything else (for example launchctl or
# file-system detail from installing the LaunchAgent) is not echoed.
_ENABLE_DIAGNOSTICS = frozenset({
    "worker_configuration_invalid", "worker_stop_unconfirmed", "worker_running_unmanaged",
    "kickoff_in_progress", "worker_lifecycle_busy", "worker_state_unavailable",
})

_RUNNING_STATES = frozenset({"starting", "running"})


def _worker_off_hint(root: Path, snapshot: dict) -> str | None:
    """The one `Next:` line for `worker status`: start the worker, resume, or turn on live tasks."""
    try:
        settings = load_worker_settings(root)
    except Exception:
        return None
    if settings.control_service_url is None:
        return None
    if not (snapshot.get("enabled") is True and snapshot.get("process_state") in _RUNNING_STATES):
        return printer.next_step(f"`openswap worker enable` to start the worker (paired with "
                                 f"{settings.control_service_url}).")
    paused = snapshot.get("paused") is True
    provider = snapshot.get("provider") or {}
    if provider.get("available") is not True and provider.get("diagnostic_code") == "live_adapter_disabled":
        from openswap.worker.accounts import provider_of
        from openswap.worker.guided_setup import execution_off_note

        # The live check refuses while the worker takes tasks: pause first unless already paused.
        note = execution_off_note(provider_of(settings.pinned_account_ref), pause_first=not paused)
        return printer.next_step(note.removeprefix("Next: "))
    if paused:
        return printer.next_step("`openswap worker pause --off` to take tasks again.")
    return None


def _format_status(snapshot: dict) -> str:
    """The human ``openswap worker status``: one aligned row per fact, each marked and worded."""
    enabled = snapshot.get("enabled") is True
    process = snapshot.get("process_state", "unavailable")
    paused = snapshot.get("paused") is True
    provider = snapshot.get("provider") or {}
    available = provider.get("available") is True
    code = provider.get("diagnostic_code")
    live = "on" if available else "off" if code in {None, "live_adapter_disabled"} else f"off ({code})"
    remote = snapshot.get("remote_connectivity", "disabled")
    seen = snapshot.get("remote_last_seen_at")
    active = snapshot.get("active_job")
    job = f"{active.get('job_id')} ({active.get('state')})" if active else "none"
    # One row for the worker: off (not enabled), else the process state.
    worker = process if enabled else "off"
    worker_ok = (False if not enabled else True if process == "running"
                 else None if process in {"starting", "stopped"} else False)
    # Only a running worker admits tasks: off, stopped or stale, it takes none whatever the pause flag.
    running = enabled and process in _RUNNING_STATES
    taking = ("paused" if paused else "yes") if running else "no (worker not running)"
    service_ok = True if remote == "online" else None if remote in {"disabled", "offline"} else False
    rows = [
        (f"{printer.mark(worker_ok)} Worker", worker),
        (f"{printer.mark(None if not running else not paused)} Taking tasks", taking),
        (f"{printer.mark(True if available else None)} Live tasks", live),
        (f"{printer.mark(service_ok)} Service", f"{remote} (seen {seen})" if seen else remote),
        (f"{printer.mark(None)} Task", job),
    ]
    return "\n".join([printer.heading("Remote tasks"), *printer.columns(rows)])
