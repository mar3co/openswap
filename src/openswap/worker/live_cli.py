"""``openswap worker codex …`` and ``openswap worker live …``: the live path's local setup.

- ``codex install [--archive PATH]`` fetches the official Codex CLI 0.157.1
  release asset, refuses it unless its SHA-256 is the published digest, and
  unpacks it into OpenSwap's private worker directory. ``codex status`` re-checks
  it and shows which accounts have an isolated sign-in.
- ``codex login [SLOT|EMAIL|ALIAS] [--device-auth]`` runs that pinned CLI's own
  ``login`` with ``CODEX_HOME`` set to the account's isolated home, so the
  owner's default ``~/.codex`` login is never touched. A sign-in to any other
  account than the one selected is signed straight back out.
- ``live status|enable|disable`` shows or changes the explicit live-execution
  opt-in. ``enable`` needs passing ``live-check`` evidence for this binary.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from openswap.exceptions import ClaudeSwitchError
from openswap.settings import load_worker_settings
from openswap.worker import codex_cli
from openswap.worker.accounts import AccountPinError, codex_accounts, resolve_codex_selector
from openswap.worker.codex_exec import codex_env, home_identity, isolated_home, managed_codex_config, prepare_home
from openswap.worker.leases import AccountLeaseError, AccountLeaseStore, ReleaseEvidence
from openswap.worker.live import (
    LiveModeError,
    disable_live,
    enable_live,
    latest_evidence,
    live_status,
)

LOGIN_LEASE_SECONDS = 15 * 60

_CLI_MESSAGES = {
    "unsupported_platform": "Remote tasks run Codex only on Apple silicon Macs.",
    "not_installed": "The pinned Codex CLI is not installed. Run `openswap worker codex install`.",
    "archive_hash_mismatch": "The downloaded archive does not match the published SHA-256; nothing was installed.",
    "download_failed": "Could not download the Codex release asset.",
    "binary_hash_mismatch": "The installed Codex binary changed since it was verified. Reinstall it.",
    "version_mismatch": "The installed binary does not report codex-cli 0.157.1. Reinstall it.",
}

_PIN_MESSAGES = {
    "managed_codex_config": ("A managed or system Codex configuration on this Mac could override where Codex "
                             "stores sign-ins, so OpenSwap won't sign accounts in or out (remote jobs refuse too)."),
}


def _message(code: str) -> str:
    return _CLI_MESSAGES.get(code, f"Refused: {code}.")


def _emit(payload: dict, as_json: bool, human: str) -> None:
    print(json.dumps(payload, sort_keys=True) if as_json else human)


@contextmanager
def selected_account_lease(backup_root: Path, selector: str | None, purpose: str):
    """Resolve ``selector`` and take that account's Codex lease atomically.

    Both happen under one hold of the Codex mutation guard, which `codex
    move`, switch and remove also take, so the slot cannot change between
    resolving it and leasing the account it named. Yields the choice.
    """
    store = AccountLeaseStore(Path(backup_root), "codex")
    with store.mutation_guard() as guard:
        choice = _resolve(Path(backup_root), selector)
        token = guard.acquire(
            job_id=f"{purpose}-{uuid.uuid4().hex}", account_identity=choice.account_ref,
            worker_pid=os.getpid(), worker_epoch=time.time_ns(), ttl_s=LOGIN_LEASE_SECONDS,
        )
    try:
        yield choice
    finally:
        store.release(token, ReleaseEvidence.CONFIRMED_STOPPED)


def _resolve(backup_root: Path, selector: str | None):
    if selector is None:
        pinned = load_worker_settings(backup_root).pinned_account_ref
        if pinned is None:
            raise AccountPinError("no_pinned_account")
        selector = pinned
    return resolve_codex_selector(backup_root, selector)


def _login_env(home: Path) -> dict[str, str]:
    env = codex_env(home, home)
    env.pop("TMPDIR", None)
    if os.environ.get("TERM"):
        env["TERM"] = os.environ["TERM"]
    return env


def _refuse_managed(home: Path, managed) -> None:
    """A managed or system Codex layer could override ``cli_auth_credentials_store``
    (and so put credentials outside the isolated home): never log in or out then."""
    if (managed or managed_codex_config)(home):
        raise AccountPinError("managed_codex_config")


def login(backup_root: Path, selector: str | None, *, device_auth: bool = False,
          run=subprocess.run, verify=None, managed=None) -> dict:
    """Sign one roster account in to its isolated home with Codex's own login."""
    root = Path(backup_root)
    pinned = (verify or (lambda: codex_cli.verify(root)))()
    argv = [str(pinned.binary), "login"] + (["--device-auth"] if device_auth else [])
    with selected_account_lease(root, selector, "login") as choice:
        # Under the lease: a job running on this account owns its home's config.
        identity = choice.account_ref
        home = prepare_home(root, identity)
        _refuse_managed(home, managed)
        env = _login_env(home)
        # From the isolated home, never the caller's directory: a project
        # `.codex` layer there could override the file-backed credential store.
        result = run(argv, env=env, check=False, cwd=str(home))
        signed_in = home_identity(home)
        if signed_in is not None and signed_in != identity:
            cleanup = run([str(pinned.binary), "logout"], env=env, check=False, capture_output=True,
                          cwd=str(home))
            if cleanup.returncode != 0 or os.path.lexists(home / "auth.json"):
                # The other account's credentials are still there: say so.
                raise AccountPinError("login_account_mismatch_still_signed_in")
            raise AccountPinError("login_account_mismatch")
    if result.returncode != 0:
        # Even with this account's (possibly stale) credentials still in the
        # home: a login that failed proves nothing about them.
        raise AccountPinError("login_failed")
    if signed_in != identity:
        raise AccountPinError("login_not_completed")
    return {"slot": choice.number, "account_ref": identity, "signed_in": True}


def logout(backup_root: Path, selector: str | None, *, run=subprocess.run, verify=None, managed=None) -> dict:
    root = Path(backup_root)
    pinned = (verify or (lambda: codex_cli.verify(root)))()
    with selected_account_lease(root, selector, "logout") as choice:
        # Under the lease, so a login in progress finishes (or fails) first.
        identity = choice.account_ref
        home = isolated_home(root, identity)
        _refuse_managed(home, managed)
        if not os.path.lexists(home / "auth.json"):
            return {"slot": choice.number, "account_ref": identity, "signed_in": False}
        result = run([str(pinned.binary), "logout"], env=_login_env(home), check=False, cwd=str(home))
        # Signed out means the credentials file is gone, not merely unreadable.
        still = os.path.lexists(home / "auth.json")
    if result.returncode != 0 or still:
        # Jobs could keep using a sign-in that is still there: never report it gone.
        raise AccountPinError("logout_failed")
    return {"slot": choice.number, "account_ref": identity, "signed_in": False}


def codex_status(backup_root: Path) -> dict:
    root = Path(backup_root)
    try:
        pinned = codex_cli.verify(root)
        cli = {"installed": True, **pinned.to_dict()}
    except codex_cli.CodexCliError as error:
        cli = {"installed": False, "problem": error.code}
    policy = load_worker_settings(root)
    allowed = {entry.identity for entry in policy.account_allowlist}
    accounts = []
    for choice in codex_accounts(root) or ():
        if choice.account_ref is None:
            continue
        home = isolated_home(root, choice.account_ref)
        accounts.append({
            "slot": choice.number, "alias": choice.alias, "account_ref": choice.account_ref,
            "pinned": choice.account_ref == policy.pinned_account_ref,
            "allowed": choice.account_ref in allowed,
            "isolated_sign_in": home_identity(home) == choice.account_ref,
        })
    return {"cli": cli, "accounts": accounts, **live_status(root)}


def _format_codex_status(status: dict) -> str:
    cli = status["cli"]
    lines = [
        f"Pinned Codex CLI: {cli['version']} (verified)" if cli["installed"]
        else f"Pinned Codex CLI: not ready ({cli['problem']})",
        f"Live execution: {status['execution_mode']}",
    ]
    for account in status["accounts"]:
        marks = []
        if account["pinned"]:
            marks.append("default")
        if account["allowed"]:
            marks.append("allowed")
        signed = "signed in" if account["isolated_sign_in"] else "not signed in"
        name = f"{account['slot']}" + (f" ({account['alias']})" if account["alias"] else "")
        lines.append(f"  {name}: {signed}" + (f" [{', '.join(marks)}]" if marks else ""))
    if not status["accounts"]:
        lines.append("  No eligible Codex accounts in the roster.")
    return "\n".join(lines)


_MUTATING = {("codex", "install"), ("codex", "login"), ("codex", "logout"), ("live", "enable"), ("live", "disable")}


def main(arguments: list[str], backup_root: Path, *, migrate=None) -> int:
    parser = argparse.ArgumentParser(prog="openswap worker", description="Live Codex setup for Remote tasks.")
    commands = parser.add_subparsers(dest="command", required=True)
    codex = commands.add_parser("codex", help="install and sign in the pinned Codex CLI for Remote tasks")
    codex_commands = codex.add_subparsers(dest="codex_command", required=True)
    install = codex_commands.add_parser("install", help="download and verify the official Codex CLI 0.157.1")
    install.add_argument("--archive", type=Path, help="use an already downloaded release archive")
    install.add_argument("--json", action="store_true")
    status = codex_commands.add_parser("status", help="re-verify the pinned CLI and list isolated sign-ins")
    status.add_argument("--json", action="store_true")
    login_parser = codex_commands.add_parser(
        "login", help="sign an account in to its own isolated Codex home (default: the pinned account)",
    )
    login_parser.add_argument("selector", nargs="?", metavar="SLOT|EMAIL|ALIAS")
    login_parser.add_argument("--device-auth", action="store_true", help="use Codex's device-code sign-in")
    login_parser.add_argument("--json", action="store_true")
    logout_parser = codex_commands.add_parser("logout", help="sign an account out of its isolated Codex home")
    logout_parser.add_argument("selector", nargs="?", metavar="SLOT|EMAIL|ALIAS")
    logout_parser.add_argument("--json", action="store_true")
    live = commands.add_parser("live", help="show or change the live-execution opt-in")
    live_commands = live.add_subparsers(dest="live_command", required=True)
    live_status_parser = live_commands.add_parser("status", help="show whether real jobs run")
    live_status_parser.add_argument("--json", action="store_true")
    enable = live_commands.add_parser("enable", help="run real jobs (needs passing live-check evidence)")
    enable.add_argument("--evidence", type=Path, help="evidence file (default: the latest live-check)")
    enable.add_argument("--json", action="store_true")
    disable = live_commands.add_parser(
        "disable", help="stop launching real jobs (a running job continues; `openswap worker stop` ends it)",
    )
    disable.add_argument("--json", action="store_true")
    args = parser.parse_args(arguments)
    root = Path(backup_root)
    try:
        sub = args.codex_command if args.command == "codex" else args.live_command
        if migrate is not None and (args.command, sub) in _MUTATING:
            # These create worker state, so legacy data must move first.
            migrate(root)
        if args.command == "codex":
            return _codex_command(root, args)
        return _live_command(root, args)
    except codex_cli.CodexCliError as error:
        print(_message(error.code), file=sys.stderr)
        return 1
    except AccountPinError as error:
        print(_PIN_MESSAGES.get(error.code, f"Refused: {error.code}."), file=sys.stderr)
        return 1
    except LiveModeError as error:
        detail = f" ({', '.join(error.problems)})" if error.problems else ""
        print(f"Refused: {error.code}{detail}.", file=sys.stderr)
        return 1
    except (AccountLeaseError, ClaudeSwitchError) as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1


def _codex_command(root: Path, args) -> int:
    if args.codex_command == "install":
        pinned = codex_cli.install(root, archive_path=args.archive)
        _emit(pinned.to_dict(), args.json,
              f"Installed {pinned.version}; archive SHA-256 matches the published digest.")
        return 0
    if args.codex_command == "status":
        status = codex_status(root)
        _emit(status, args.json, _format_codex_status(status))
        return 0 if status["cli"]["installed"] else 1
    if args.codex_command == "login":
        result = login(root, args.selector, device_auth=args.device_auth)
        _emit(result, args.json, f"Codex account {result['slot']} is signed in to its isolated home. "
                                 "Your default Codex login was not changed.")
        return 0
    result = logout(root, args.selector)
    _emit(result, args.json, f"Codex account {result['slot']} is signed out of its isolated home.")
    return 0


def _live_command(root: Path, args) -> int:
    if args.live_command == "status":
        status = live_status(root)
        _emit(status, args.json, f"Live execution: {status['execution_mode']}")
        return 0
    if args.live_command == "disable":
        disable_live(root)
        status = live_status(root)
        _emit(status, args.json, "Live execution is off: no new job will launch (they fail with "
                                 "live_adapter_disabled). A job already running continues; "
                                 "`openswap worker stop` ends it.")
        return 0
    evidence = args.evidence or latest_evidence(root)
    if evidence is None:
        print("No live-check evidence found. Run `openswap worker live-check` first.", file=sys.stderr)
        return 1
    pinned = codex_cli.verify(root)
    enable_live(root, evidence, pinned)
    status = live_status(root)
    _emit(status, args.json, "Live execution is on: remote jobs run with the pinned Codex CLI on "
                             "the isolated sign-in of their account. `openswap worker live disable` turns it off.")
    return 0
