"""Early, credential-free CLI boundary for local worker operations."""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import json
import re
import sys
import time
from pathlib import Path

from openswap import paths
from openswap.exceptions import ClaudeSwitchError
from openswap.locking import FileLock
from openswap.paths import get_backup_root
from openswap.settings import load_worker_settings, update_worker_settings
from openswap.worker.client import WorkerClient
from openswap.worker.ipc import IpcError, socket_path
from openswap.worker.journal import LocalJobStore
from openswap.worker.launch_agent import install, uninstall
from openswap.worker.launch_agent import status as worker_service_status
from openswap.worker.leases import START_PENDING_REASON, AccountLeaseStore, ReleaseEvidence
from openswap.worker.models import JobState
from openswap.worker.runtime import _pid_exists, read_worker_snapshot

_DISABLE_WAIT_SECONDS = 3.0
_DISABLE_POLL_SECONDS = 0.1
_DISABLE_EXIT_WAIT_SECONDS = 3.0
_LIFECYCLE_LOCK_TIMEOUT_SECONDS = 5.0
# Terminal states that carry stop evidence. INTERRUPTED is terminal but records
# uncertainty (the provider may have survived), so it needs owner confirmation.
# Journaled worker jobs have 32-hex ids; kickoff-* and usage-* probe leases do not.
_WORKER_JOB_ID = re.compile(r"[a-f0-9]{32}")
_LEASE_STOP_PROVEN_JOB_STATES = {
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
      started, or its pid is no longer alive) and its job in a terminal state
      that proves a stop;
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
                        job_state in _LEASE_STOP_PROVEN_JOB_STATES
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
                elif job_state not in _LEASE_STOP_PROVEN_JOB_STATES:
                    return False, {}, "job_not_terminal"
            guard.release(lease.token(), ReleaseEvidence.OWNER_RELEASED)
            return True, {"lease_state": "released"}, None


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
    parser = argparse.ArgumentParser(
        prog="openswap worker",
        description="Control the opt-in, local-only Remote Agent Host worker.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run the background worker process")
    # Passed only by the LaunchAgent: a manual run refuses while it is loaded.
    run.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)
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
    args = parser.parse_args(argv)
    root = Path(backup_root) if backup_root is not None else get_backup_root()

    if args.command in {"run", "enable", "disable", "pause"}:
        try:
            _migrate_legacy_before_worker_state_change(root)
        except ClaudeSwitchError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    if args.command == "run":
        return _run(root, managed=args.managed)
    if args.command == "status":
        try:
            snapshot = read_status(root)
        except Exception:
            print("Worker status unavailable.", file=sys.stderr)
            return 1
        _write(snapshot, as_json=args.json, human=_format_status(snapshot))
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
            message = (
                "Worker configuration is invalid; fix local worker settings before enabling."
                if str(exc) == "worker_configuration_invalid"
                else "Worker is still stopping; wait for it to exit before enabling."
                if str(exc) == "worker_stop_unconfirmed"
                else "Could not enable worker."
            )
            print(message, file=sys.stderr)
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


def _format_status(snapshot: dict) -> str:
    enabled = "enabled" if snapshot.get("enabled") is True else "disabled"
    process = snapshot.get("process_state", "unavailable")
    admission = "paused" if snapshot.get("paused") is True else "open"
    provider = snapshot.get("provider") or {}
    provider_state = (
        "available" if provider.get("available") is True
        else provider.get("diagnostic_code") or "unavailable"
    )
    active = snapshot.get("active_job")
    job = f"; job {active.get('job_id')} ({active.get('state')})" if active else ""
    return (
        f"Remote tasks: {enabled}; worker: {process}; admission: {admission}; "
        f"provider: {provider_state}{job}"
    )
