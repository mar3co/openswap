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

from openswap import printer
from openswap.exceptions import ClaudeSwitchError
from openswap.settings import load_worker_settings
from openswap.worker import codex_cli
from openswap.worker.accounts import AccountPinError, codex_accounts, resolve_codex_selector
from openswap.worker.codex_exec import codex_env, home_identity, isolated_home, managed_codex_config, prepare_home
from openswap.worker.leases import AccountLeaseError, AccountLeaseStore, ReleaseEvidence
from openswap.worker.claude_cli import ClaudeCliError
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
    "claude_settings_invalid": ("That account's profile has a settings.json Claude Code would ignore (not valid "
                                "JSON, a symlink, or permission keys it does not accept). Remote tasks refuse to "
                                "run until it is fixed or removed."),
    "claude_sign_in_not_in_profile": ("Claude Code signed in, but did not keep the sign-in in the profile's "
                                      "credentials file, which is the only place remote tasks can use it."),
    "claude_default_settings_missing": "~/.claude/settings.json has no permission settings to copy.",
    "codex_settings_invalid": ("That account's remote-task settings (permissions.json in its isolated Codex "
                               "home) are not valid. Set them again with `openswap worker codex settings`."),
    "codex_default_settings_missing": ("~/.codex/config.toml sets no approval_policy, approvals_reviewer or "
                                       "sandbox_mode to copy."),
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
          run=subprocess.run, verify=None, managed=None, settings=None) -> dict:
    """Sign one roster account in to its isolated home with Codex's own login.

    ``settings`` (a :class:`~openswap.worker.permissions.CodexPermissions`) is
    recorded for the account's remote tasks after a successful sign-in, under
    the same account lease, so no job can start in between on the old ones.
    """
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
        if settings is not None and result.returncode == 0 and signed_in == identity:
            from openswap.worker.permissions import write_codex_permissions

            write_codex_permissions(home, settings)
    if result.returncode != 0:
        # Even with this account's (possibly stale) credentials still in the
        # home: a login that failed proves nothing about them.
        raise AccountPinError("login_failed")
    if signed_in != identity:
        raise AccountPinError("login_not_completed")
    out = {"slot": choice.number, "account_ref": identity, "signed_in": True}
    if settings is not None:
        out["permissions"] = settings.to_dict() | {"recorded": True}
    return out


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


def codex_settings(backup_root: Path, selector: str | None, *, copy_settings: bool = False,
                   approval: str | None = None, sandbox: str | None = None, reviewer: str | None = None,
                   codex_home: Path | None = None) -> dict:
    """Show or set an account's Codex approval and sandbox settings for remote tasks.

    ``copy_settings`` takes ``approval_policy``, ``approvals_reviewer`` and
    ``sandbox_mode`` from the owner's ``~/.codex/config.toml`` (and its
    selected profile); the explicit values then override them. Written under
    the account's Codex lease, so no job of it is running meanwhile.
    """
    from dataclasses import replace

    from openswap.worker.permissions import (
        CODEX_APPROVALS, CODEX_REVIEWERS, CODEX_SANDBOXES, CodexPermissions, PermissionSettingsError,
        default_codex_permissions, read_codex_permissions, write_codex_permissions,
    )

    root = Path(backup_root)
    if ((approval is not None and approval not in CODEX_APPROVALS)
            or (sandbox is not None and sandbox not in CODEX_SANDBOXES)
            or (reviewer is not None and reviewer not in CODEX_REVIEWERS)):
        raise AccountPinError("codex_settings_value_invalid")
    changing = copy_settings or approval is not None or sandbox is not None or reviewer is not None
    if not changing:
        # Showing needs no lease (a running job keeps it).
        choice = _resolve(root, selector)
        try:
            current = read_codex_permissions(isolated_home(root, choice.account_ref))
        except PermissionSettingsError:
            raise AccountPinError("codex_settings_invalid") from None
        return {"slot": choice.number, "account_ref": choice.account_ref, "permissions": current.to_dict(),
                "changed": False}
    with selected_account_lease(root, selector, "settings") as choice:
        home = isolated_home(root, choice.account_ref)
        try:
            base = read_codex_permissions(home)
        except PermissionSettingsError:
            base = CodexPermissions()  # being replaced
        if copy_settings:
            copied = default_codex_permissions(codex_home)
            if copied is None:
                raise AccountPinError("codex_default_settings_missing")
            base = copied
        base = replace(base, **{key: value for key, value in (("approval", approval), ("sandbox", sandbox),
                                                               ("reviewer", reviewer)) if value is not None})
        prepare_home(root, choice.account_ref)  # the isolated home, 0700, if it is not there yet
        current = write_codex_permissions(home, base)
    return {"slot": choice.number, "account_ref": choice.account_ref, "permissions": current.to_dict(),
            "changed": True}


def _override(root: Path) -> str:
    from openswap.settings import load_permission_override

    try:
        return load_permission_override(root)
    except Exception:
        return "read-only"


def codex_status(backup_root: Path) -> dict:
    from openswap.worker.permissions import PermissionSettingsError, read_codex_permissions

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
        try:
            permissions = read_codex_permissions(home).to_dict()
        except PermissionSettingsError:
            permissions = None
        accounts.append({
            "slot": choice.number, "alias": choice.alias, "account_ref": choice.account_ref,
            "pinned": choice.account_ref == policy.pinned_account_ref,
            "allowed": choice.account_ref in allowed,
            "isolated_sign_in": home_identity(home) == choice.account_ref,
            # The approval and sandbox settings remote tasks follow (None: unreadable).
            "permissions": permissions,
        })
    return {"cli": cli, "accounts": accounts, **live_status(root), "permission_override": _override(root)}


def _account_status_rows(accounts, ready_key: str, ready: str, not_ready: str,
                         describe=None) -> list[tuple[str, ...]]:
    rows = []
    for account in accounts:
        roles = [m for m, on in (("default", account["pinned"]), ("allowed", account["allowed"])) if on]
        row = (f"{printer.mark(account[ready_key])} {account['slot']}",
               f"({account['alias']})" if account["alias"] else "",
               ready if account[ready_key] else not_ready, ", ".join(roles))
        if describe is not None:
            # What its remote tasks may do (the account's own settings).
            permissions = account.get("permissions")
            row += (describe(permissions) if permissions is not None else "settings unreadable",)
        rows.append(row)
    return rows


def _override_line(status: dict) -> str | None:
    """The per-Mac limit, shown only when it is not the default."""
    from openswap.worker.permissions import FOLLOW, OVERRIDE_DESCRIPTIONS

    override = status.get("permission_override", FOLLOW)
    if override == FOLLOW:
        return None
    return f"  {printer.mark(None)} This Mac limits every remote task: {OVERRIDE_DESCRIPTIONS[override]}."


def _format_codex_status(status: dict) -> str:
    """``codex status``: the pinned CLI, the opt-in, one row per account, then the next step."""
    cli = status["cli"]
    live = status["execution_mode"] == "live"
    lines = [printer.heading("Codex for Remote tasks"), *printer.columns([
        (f"{printer.mark(cli['installed'])} Pinned CLI",
         f"{cli['version']} (verified)" if cli["installed"] else f"not ready ({cli['problem']})"),
        (f"{printer.mark(True if live else None)} Live execution", status["execution_mode"]),
    ]), printer.heading("Accounts (isolated sign-in; slot and alias)")]
    if not status["accounts"]:
        lines.append("  No eligible Codex accounts in the roster. Add one with `openswap codex add`.")
    lines.extend(printer.columns(_account_status_rows(status["accounts"], "isolated_sign_in",
                                                      "signed in", "not signed in", _codex_describe)))
    override = _override_line(status)
    if override is not None:
        lines.append(override)
    lines.append(printer.next_step(_codex_next_step(status)))
    return "\n".join(lines)


def _unchecked(status: dict, account: dict) -> bool:
    """Whether a live check is still missing for this account (only known when the status says)."""
    checked = status.get("checked_accounts")
    return checked is not None and account.get("account_ref") not in checked


def _codex_next_step(status: dict) -> str:
    accounts = status["accounts"]
    pinned = next((a for a in accounts if a["pinned"]), None)
    allowed = [a for a in accounts if a["allowed"] and not a["pinned"]]
    if not status["cli"]["installed"]:
        return "install the pinned CLI: `openswap worker codex install`."
    if pinned is None:
        # The live check runs on the pinned account: pin one first.
        return "pin the Codex account remote jobs run on: `openswap worker account <slot>`."
    if not pinned["isolated_sign_in"]:
        return "sign the default account in: `openswap worker codex login`."
    unsigned = next((a for a in allowed if not a["isolated_sign_in"]), None)
    if unsigned is not None:
        return f"sign allowed account {unsigned['slot']} in: `openswap worker codex login {unsigned['slot']}`."
    if status.get("recheck_needed"):
        return ("run the live check again (remote tasks now follow each account's own settings): "
                "`openswap worker live-check`.")
    if status["execution_mode"] != "live":
        return "run the live check and enable live execution: `openswap worker live-check`."
    unchecked = next((a for a in [pinned, *allowed] if _unchecked(status, a)), None)
    if unchecked is not None:
        return (f"run the live check on account {unchecked['slot']} too: "
                f"`openswap worker live-check --account {unchecked['slot']}`.")
    return "nothing: live execution is on. `openswap worker live disable` turns it off."


def _claude_next_step(status: dict) -> str:
    accounts = status["accounts"]
    pinned = next((a for a in accounts if a["pinned"]), None)
    allowed = [a for a in accounts if a["allowed"] and not a["pinned"]]
    if not status["cli"]["pinned"]:
        return "pin the installed Claude Code: `openswap worker claude pin`."
    if pinned is None:
        return "pin the Claude account remote jobs run on: `openswap worker account claude:<slot>`."
    if not pinned["profile_ready"]:
        return "prepare the default account's profile: `openswap worker claude prepare`."
    unready = next((a for a in allowed if not a["profile_ready"]), None)
    if unready is not None:
        return (f"prepare allowed account {unready['slot']}'s profile: "
                f"`openswap worker claude prepare claude:{unready['slot']}`.")
    if status.get("recheck_needed"):
        return ("run the live check again (remote tasks now follow each account's own settings): "
                "`openswap worker live-check --provider claude`.")
    if status["execution_mode"] != "live":
        return "run the live check and enable live execution: `openswap worker live-check --provider claude`."
    unchecked = next((a for a in [pinned, *allowed] if _unchecked(status, a)), None)
    if unchecked is not None:
        return (f"run the live check on account {unchecked['slot']} too: "
                f"`openswap worker live-check --provider claude --account claude:{unchecked['slot']}`.")
    return "nothing: live execution is on. `openswap worker live disable --provider claude` turns it off."


_MUTATING = {("codex", "install"), ("codex", "login"), ("codex", "logout"), ("codex", "settings"),
             ("live", "enable"), ("live", "disable"), ("claude", "pin"), ("claude", "prepare")}


def _unshare_profile(profile: Path) -> None:
    """Remove what scheduled kickoff mirrored from ``~/.claude`` (links and MCP mirror only)."""
    from openswap.session import SessionManager
    from openswap.switcher import ClaudeAccountSwitcher

    SessionManager(ClaudeAccountSwitcher())._sync_sharing(profile, share=False, share_history=False)


def _claude_login_env(profile: Path) -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(Path.home()),
        # The exact string jobs export: Claude Code derives the profile's own
        # Keychain item from it, so the default login's item is never used.
        "CLAUDE_CONFIG_DIR": str(profile),
        "DISABLE_AUTOUPDATER": "1",
    }
    for name in ("TERM", "USER", "LOGNAME", "LANG"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def _keychain_free_run(root: Path, run, argv: list[str], **kwargs):
    """Run ``argv`` under a Seatbelt profile with no Keychain (see ``keychain_free_profile``)."""
    from openswap.worker.claude_exec import keychain_free_profile
    from openswap.worker.containment import ensure_private_dir, write_private

    worker = root / "worker"
    ensure_private_dir(worker)
    profile = worker / "claude-sign-in.sb"
    write_private(profile, keychain_free_profile().encode("utf-8"))
    return run(["/usr/bin/sandbox-exec", "-f", str(profile), *argv], **kwargs)


def claude_prepare(backup_root: Path, selector: str | None, *, run=subprocess.run, verify=None,
                   unshare=None, copy_settings: bool = False, mode: str | None = None, decide=None,
                   home: Path | None = None) -> dict:
    """Get a Claude account's OpenSwap profile ready for remote jobs, with Claude's own login.

    The profile is the account's OpenSwap session folder (plan 003). If it is
    not signed in as this account, or its sign-in is only in the Keychain
    (which jobs cannot reach), the pinned Claude Code signs in there itself
    (`claude auth login`, with ``CLAUDE_CONFIG_DIR`` set to the profile) with
    the Keychain out of reach, so Claude Code keeps the sign-in in the
    profile's credentials file. OpenSwap never reads, copies or seeds a
    credential, and the default login (``~/.claude`` and its Keychain item) is
    never touched. Customizations a scheduled kickoff mirrored into it are
    removed. The Claude lease is held throughout, so no job launches on the
    profile meanwhile.

    Permission settings (owner decision 2026-10-08): ``copy_settings`` copies
    the mode and the allow, deny and ask rules from ``~/.claude/settings.json``
    into the profile's ``settings.json``; ``mode`` then sets the mode. With
    neither, ``decide(current, default)`` (interactive setup) may return
    ``(copy, mode)`` for a profile that has no permission settings yet.
    """
    from openswap.worker import claude_cli
    from openswap.worker.accounts import resolve_account_selector, resolve_claude_selector
    from openswap.worker.claude_exec import (
        credentials_in_file, managed_claude_config, profile_for, profile_identity, profile_shared,
        profile_symlinked,
    )
    from openswap.worker.permissions import (
        CLAUDE_MODES, PermissionSettingsError, default_claude_permissions, read_claude_permissions,
        write_claude_permissions,
    )

    root = Path(backup_root)
    if selector is None:
        pinned = load_worker_settings(root).pinned_account_ref
        if not isinstance(pinned, str) or not pinned.startswith("claude:"):
            raise AccountPinError("no_pinned_claude_account")
        selector = pinned
    try:
        choice = resolve_account_selector(root, selector)
    except AccountPinError:
        choice = None
    if choice is None or choice.provider != "claude" or not getattr(choice, "email", None):
        # A bare slot means a Codex slot first; here only Claude makes sense.
        choice = resolve_claude_selector(root, selector if choice is None or choice.provider != "claude"
                                         else choice.account_ref)
    identity = choice.account_ref
    profile = profile_for(root, identity)
    if profile is None:
        raise AccountPinError("account_not_found")

    if profile_symlinked(root, profile):
        # Jobs refuse a symlinked profile; never prepare (or log in to) one.
        raise AccountPinError("claude_profile_unsafe")

    def signed_in() -> bool:
        return profile.is_dir() and profile_identity(profile) == identity and credentials_in_file(profile)

    def ready() -> bool:
        return signed_in() and not profile_shared(profile) and not managed_claude_config(profile)

    def current_permissions():
        try:
            return read_claude_permissions(profile) if profile.is_dir() else None
        except PermissionSettingsError:
            # Launches refuse it too; only the owner can say what they meant.
            raise AccountPinError("claude_settings_invalid") from None

    if mode is not None and mode not in CLAUDE_MODES:
        raise AccountPinError("mode_invalid")
    store = AccountLeaseStore(root, "claude")
    with store.mutation_guard() as guard:
        # Refuse while a job (or anything else) holds a Claude lease; if the
        # profile needs any change, take the lease before letting go of the
        # guard, so no launch can interleave with the cleanup, the login or a
        # settings change.
        guard.assert_available()
        current = current_permissions()
        asking = decide is not None and not copy_settings and mode is None and (
            current is None or not current.configured)
        if ready() and not (copy_settings or mode is not None or asking):
            return {"slot": choice.number, "account_ref": identity, "profile_ready": True, "signed_in_now": False,
                    "permissions": current.to_dict() if current is not None else None}
        token = guard.acquire(job_id=f"prepare-{uuid.uuid4().hex}", account_identity=identity,
                              worker_pid=os.getpid(), worker_epoch=time.time_ns(), ttl_s=LOGIN_LEASE_SECONDS)
    signed_in_now = False
    try:
        pinned = (verify or (lambda: claude_cli.verify(root)))()
        if profile.is_dir() and profile_shared(profile):
            (unshare or _unshare_profile)(profile)
        if not signed_in():
            profile.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            profile.mkdir(mode=0o700, exist_ok=True)
            env = _claude_login_env(profile)
            # Without the Keychain, so the sign-in lands in the profile's file.
            result = _keychain_free_run(root, run, [str(pinned.binary), "auth", "login", "--claudeai", "--email",
                                                    choice.email], env=env, check=False)
            signed = profile_identity(profile)
            if signed is not None and signed != identity:
                # Signed in as someone else: sign that account back out of the profile.
                cleanup = _keychain_free_run(root, run, [str(pinned.binary), "auth", "logout"], env=env,
                                             check=False, capture_output=True)
                if cleanup.returncode != 0 or profile_identity(profile) is not None:
                    raise AccountPinError("login_account_mismatch_still_signed_in")
                raise AccountPinError("login_account_mismatch")
            if result.returncode != 0:
                raise AccountPinError("login_failed")
            if not credentials_in_file(profile):
                # Claude Code kept the sign-in somewhere jobs cannot reach.
                raise AccountPinError("claude_sign_in_not_in_profile")
            signed_in_now = True
        current = current_permissions()
        if asking:
            copy_settings, mode = decide(current, default_claude_permissions(home))
        if copy_settings or mode is not None:
            copied = default_claude_permissions(home) if copy_settings else None
            if copy_settings and copied is None:
                raise AccountPinError("claude_default_settings_missing")
            try:
                current = write_claude_permissions(profile, copied=copied, mode=mode)
            except (OSError, PermissionSettingsError):
                raise AccountPinError("claude_settings_invalid") from None
    finally:
        store.release(token, ReleaseEvidence.CONFIRMED_STOPPED)
    if not ready():
        raise AccountPinError("claude_profile_not_ready")
    return {"slot": choice.number, "account_ref": identity, "profile_ready": True, "signed_in_now": signed_in_now,
            "permissions": current.to_dict() if current is not None else None}


def claude_status(backup_root: Path) -> dict:
    from openswap.worker import claude_cli
    from openswap.worker.accounts import claude_accounts
    from openswap.worker.claude_exec import (
        credentials_in_file, managed_claude_config, profile_for, profile_identity, profile_shared,
        profile_symlinked,
    )
    from openswap.worker.permissions import PermissionSettingsError, read_claude_permissions

    root = Path(backup_root)
    try:
        pinned = claude_cli.verify(root)
        cli = {"pinned": True, **pinned.to_dict()}
    except claude_cli.ClaudeCliError as error:
        cli = {"pinned": False, "problem": error.code}
    policy = load_worker_settings(root)
    allowed = {entry.identity for entry in policy.account_allowlist}
    accounts = []
    for choice in claude_accounts(root) or ():
        if choice.account_ref is None:
            continue
        profile = profile_for(root, choice.account_ref)
        try:
            permissions = read_claude_permissions(profile).to_dict() if profile is not None else None
        except PermissionSettingsError:
            permissions = None
        accounts.append({
            "slot": choice.number, "alias": choice.alias, "account_ref": choice.account_ref,
            "pinned": choice.account_ref == policy.pinned_account_ref,
            "allowed": choice.account_ref in allowed,
            # Exactly what a launch requires: signed in as the account, in
            # the profile's credentials file, with settings a launch can
            # trust, and not mirroring the default profile's customizations.
            "profile_ready": profile is not None and not profile_symlinked(root, profile)
            and profile_identity(profile) == choice.account_ref and credentials_in_file(profile)
            and permissions is not None
            and not profile_shared(profile) and not managed_claude_config(profile),
            # The mode and rule counts remote tasks follow (None: unreadable settings).
            "permissions": permissions,
        })
    return {"cli": cli, "accounts": accounts, **live_status(root, "claude"),
            "permission_override": _override(root)}


def main(arguments: list[str], backup_root: Path, *, migrate=None) -> int:
    parser = argparse.ArgumentParser(prog="openswap worker", description="Live Codex and Claude setup for Remote tasks.")
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
    login_parser.add_argument("--copy-settings", action="store_true",
                              help="also use your ~/.codex/config.toml approval and sandbox settings for this "
                                   "account's remote tasks")
    login_parser.add_argument("--json", action="store_true")
    settings_parser = codex_commands.add_parser(
        "settings", help="show or set the approval and sandbox settings an account's remote tasks follow",
        description="Remote tasks on a Codex account follow its own approval policy, reviewer and sandbox "
                    "mode, like `codex` run there. Nobody is at the Mac to approve, so whatever would ask is "
                    "denied (unless the reviewer is auto_review). OpenSwap's folder rules hold in every mode.",
    )
    settings_parser.add_argument("selector", nargs="?", metavar="SLOT|EMAIL|ALIAS")
    settings_parser.add_argument("--copy-settings", action="store_true",
                                 help="copy approval_policy, approvals_reviewer and sandbox_mode from "
                                      "~/.codex/config.toml")
    settings_parser.add_argument("--approval", choices=("never", "on-request", "on-failure", "untrusted"))
    settings_parser.add_argument("--sandbox", choices=("read-only", "workspace-write", "danger-full-access"))
    settings_parser.add_argument("--reviewer", choices=("user", "auto_review", "guardian_subagent"))
    settings_parser.add_argument("--json", action="store_true")
    logout_parser = codex_commands.add_parser("logout", help="sign an account out of its isolated Codex home")
    logout_parser.add_argument("selector", nargs="?", metavar="SLOT|EMAIL|ALIAS")
    logout_parser.add_argument("--json", action="store_true")
    claude = commands.add_parser("claude", help="pin the Claude Code CLI and prepare account profiles for Remote tasks")
    claude_commands = claude.add_subparsers(dest="claude_command", required=True)
    claude_pin = claude_commands.add_parser("pin", help="record the installed Claude Code binary's version and SHA-256")
    claude_pin.add_argument("--binary", type=Path, help="pin this binary instead of the installed `claude`")
    claude_pin.add_argument("--json", action="store_true")
    claude_status_parser = claude_commands.add_parser("status", help="re-verify the pinned binary and list profiles")
    claude_status_parser.add_argument("--json", action="store_true")
    prepare = claude_commands.add_parser(
        "prepare", help="prepare an account's OpenSwap session profile (default: the pinned Claude account)",
    )
    prepare.add_argument("selector", nargs="?", metavar="SLOT|EMAIL|ALIAS")
    prepare.add_argument("--copy-settings", action="store_true",
                         help="copy the permission mode and allow/deny/ask rules from ~/.claude/settings.json "
                              "into the profile (remote tasks follow the profile's settings)")
    prepare.add_argument("--mode", choices=("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"),
                         help="set the permission mode remote tasks on this account use")
    prepare.add_argument("--json", action="store_true")
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
    for sub in (live_status_parser, enable, disable):
        sub.add_argument("--provider", choices=("codex", "claude"), default="codex",
                         help="which provider's opt-in (default: codex)")
    args = parser.parse_args(arguments)
    root = Path(backup_root)
    try:
        sub = {"codex": getattr(args, "codex_command", None), "claude": getattr(args, "claude_command", None)}.get(
            args.command, getattr(args, "live_command", None))
        if migrate is not None and (args.command, sub) in _MUTATING:
            # These create worker state, so legacy data must move first.
            migrate(root)
        if args.command == "codex":
            return _codex_command(root, args)
        if args.command == "claude":
            return _claude_command(root, args)
        return _live_command(root, args)
    except codex_cli.CodexCliError as error:
        print(_message(error.code), file=sys.stderr)
        return 1
    except ClaudeCliError as error:
        print(_CLAUDE_MESSAGES.get(error.code, f"Refused: {error.code}."), file=sys.stderr)
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
              f"{printer.MARK_OK} Installed {pinned.version}; archive SHA-256 matches the published digest.\n"
              + printer.next_step("sign the pinned account in: `openswap worker codex login`."))
        return 0
    if args.codex_command == "status":
        status = codex_status(root)
        _emit(status, args.json, _format_codex_status(status))
        return 0 if status["cli"]["installed"] else 1
    if args.codex_command == "login":
        from openswap.worker.permissions import default_codex_permissions

        # Decided before the sign-in, and recorded with it under one account
        # lease, so no job can start on the account in between.
        settings = None
        if args.copy_settings:
            settings = default_codex_permissions()
            if settings is None:
                raise AccountPinError("codex_default_settings_missing")
        elif not args.json and _interactive():
            settings = _offer_codex_copy(root, args.selector)
        result = login(root, args.selector, device_auth=args.device_auth, settings=settings)
        lines = [f"{printer.MARK_OK} Codex account {result['slot']} is signed in to its isolated home. "
                 "Your default Codex login was not changed."]
        if settings is not None:
            lines.append(f"{printer.MARK_OK} Remote tasks on it follow {_codex_describe(result['permissions'])}.")
        _emit(result, args.json, "\n".join(lines) + "\n"
              + printer.next_step("run the live check: `openswap worker live-check`."))
        return 0
    if args.codex_command == "settings":
        result = codex_settings(root, args.selector, copy_settings=args.copy_settings, approval=args.approval,
                                sandbox=args.sandbox, reviewer=args.reviewer)
        verb = "now follow" if result["changed"] else "follow"
        _emit(result, args.json, f"{printer.MARK_OK} Remote tasks on Codex account {result['slot']} {verb} "
                                 f"{_codex_describe(result['permissions'])}.\n" + _HEADLESS_NOTE)
        return 0
    result = logout(root, args.selector)
    _emit(result, args.json, f"{printer.MARK_OK} Codex account {result['slot']} is signed out of its isolated home.")
    return 0


_CLAUDE_MESSAGES = {
    "version_unsupported": ("That Claude Code is older than 2.1.7, which lets a symlink get around permission "
                            "deny rules. Update it, then run `openswap worker claude pin` again."),
    "unsupported_platform": "Remote tasks run Claude Code only on Apple silicon Macs.",
    "not_installed": "Claude Code is not installed. Install it, then run `openswap worker claude pin`.",
    "not_pinned": "No Claude Code binary is pinned. Run `openswap worker claude pin`.",
    "binary_changed": ("Claude Code changed since it was pinned (an update). Run `openswap worker claude pin` "
                       "and `openswap worker live-check --provider claude` again."),
    "binary_permissions": "The Claude Code binary is writable by others; it can't be pinned.",
    "binary_in_claude_config": ("That Claude Code is installed inside ~/.claude, which remote jobs can't read. "
                                "Install it with Homebrew or the native installer, then pin again."),
}


def _claude_command(root: Path, args) -> int:
    from openswap.worker import claude_cli

    if args.claude_command == "pin":
        pinned = claude_cli.pin(root, binary=args.binary)
        _emit(pinned.to_dict(), args.json,
              f"{printer.MARK_OK} Pinned Claude Code {pinned.version} ({pinned.binary_sha256[:12]}…).\n"
              + printer.next_step("prepare the account's profile: `openswap worker claude prepare`, then "
                                  "`openswap worker live-check --provider claude`."))
        return 0
    if args.claude_command == "status":
        status = claude_status(root)
        _emit(status, args.json, _format_claude_status(status))
        return 0 if status["cli"]["pinned"] else 1
    if not args.json:
        print("If that account is not signed in to its OpenSwap profile yet, Claude Code opens its own "
              "sign-in in your browser.")
    decide = _ask_claude_settings if not args.json and _interactive() else None
    result = claude_prepare(root, args.selector, copy_settings=args.copy_settings, mode=args.mode, decide=decide)
    permissions = result.get("permissions") or {}
    _emit(result, args.json, f"{printer.MARK_OK} Claude account {result['slot']}'s OpenSwap profile is ready "
                             "for remote jobs. Your default Claude login was not changed.\n"
                             f"{printer.MARK_OK} Remote tasks on it follow its own settings: "
                             f"{_claude_describe(permissions)}.\n" + _HEADLESS_NOTE + "\n"
                             + printer.next_step("run the live check: `openswap worker live-check --provider claude`."))
    return 0


_HEADLESS_NOTE = ("Nobody is at this Mac to approve, so anything that would ask is denied. "
                  "`openswap worker permissions` can limit every remote task on this Mac.")


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _yes(question: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {hint} ").strip().lower()
    except EOFError:
        return False
    return default if not answer else answer in {"y", "yes"}


def _claude_describe(permissions: dict) -> str:
    from openswap.worker.permissions import ClaudePermissions

    if not permissions:
        return "Claude Code's defaults"
    return ClaudePermissions(permissions.get("mode"), permissions.get("allow_rules", 0),
                             permissions.get("deny_rules", 0), permissions.get("ask_rules", 0)).describe()


def _codex_describe(permissions: dict) -> str:
    from openswap.worker.permissions import CodexPermissions

    return CodexPermissions(permissions["approval_policy"], permissions["sandbox_mode"],
                            permissions["approvals_reviewer"], permissions["recorded"]).describe()


def _ask_claude_settings(current, default) -> tuple[bool, str | None]:
    """Interactive `claude prepare`, for a profile with no permission settings yet."""
    from openswap.worker.permissions import CLAUDE_MODES, ClaudePermissions

    copy = False
    if default is not None:
        summary = ClaudePermissions(default.get("defaultMode"), len(default.get("allow", [])),
                                    len(default.get("deny", [])), len(default.get("ask", []))).describe()
        copy = _yes(f"Remote tasks follow this account's own Claude Code permission settings. Copy yours "
                    f"from ~/.claude/settings.json ({summary})?")
    mode_now = ((default or {}).get("defaultMode") if copy else None) or "Claude Code's default"
    try:
        answer = input(f"Permission mode for remote tasks on this account ({', '.join(CLAUDE_MODES)}; "
                       f"Enter keeps {mode_now}): ").strip()
    except EOFError:
        answer = ""
    mode = answer if answer in CLAUDE_MODES else None
    if answer and mode is None:
        print(f"Not a mode; keeping {mode_now}. Change it later with "
              "`openswap worker claude prepare --mode <mode>`.")
    return copy, mode


def _offer_codex_copy(root: Path, selector: str | None):
    """Before a sign-in: offer the owner's own Codex approval and sandbox settings, once.

    The settings to record with the sign-in, or None.
    """
    from openswap.worker.permissions import PermissionSettingsError, default_codex_permissions, read_codex_permissions

    try:
        choice = _resolve(root, selector)
        if read_codex_permissions(isolated_home(root, choice.account_ref)).recorded:
            return None
    except (AccountPinError, PermissionSettingsError):
        return None
    default = default_codex_permissions()
    if default is None:
        return None
    if not _yes(f"Remote tasks follow this account's Codex approval and sandbox settings. Use yours from "
                f"~/.codex/config.toml ({default.describe()})?"):
        return None
    return default


def _format_claude_status(status: dict) -> str:
    """``claude status``: the pinned binary, the opt-in, one row per account, then the next step."""
    cli = status["cli"]
    live = status["execution_mode"] == "live"
    lines = [printer.heading("Claude Code for Remote tasks"), *printer.columns([
        (f"{printer.mark(cli['pinned'])} Pinned binary",
         f"{cli['version']} (verified)" if cli["pinned"] else f"not ready ({cli['problem']})"),
        (f"{printer.mark(True if live else None)} Live execution", status["execution_mode"]),
    ]), printer.heading("Accounts (OpenSwap profile; pin with `claude:<slot>`)")]
    if not status["accounts"]:
        lines.append("  No eligible Claude accounts in the roster. Add one with `openswap add`.")
    lines.extend(printer.columns(_account_status_rows(status["accounts"], "profile_ready",
                                                      "profile ready", "profile not prepared", _claude_describe)))
    override = _override_line(status)
    if override is not None:
        lines.append(override)
    lines.append(printer.next_step(_claude_next_step(status)))
    return "\n".join(lines)


def _live_command(root: Path, args) -> int:
    provider = args.provider
    flag = " --provider claude" if provider == "claude" else ""
    if args.live_command == "status":
        status = live_status(root, provider)
        live = status["execution_mode"] == "live"
        human = f"{printer.mark(True if live else None)} Live execution ({provider}): {status['execution_mode']}"
        if not live:
            human += "\n" + printer.next_step(f"run the live check and enable live execution: "
                                               f"`openswap worker live-check{flag}`.")
        _emit(status, args.json, human)
        return 0
    if args.live_command == "disable":
        disable_live(root, provider)
        status = live_status(root, provider)
        _emit(status, args.json, f"{printer.MARK_OK} Live execution is off: no new job will launch (they fail with "
                                 "live_adapter_disabled). A job already running continues; "
                                 "`openswap worker stop` ends it.")
        return 0
    evidence = args.evidence or latest_evidence(root, provider)
    if evidence is None:
        print(f"No {provider} live-check evidence found. Run `openswap worker live-check --provider {provider}` first.",
              file=sys.stderr)
        return 1
    if provider == "claude":
        from openswap.worker import claude_cli

        pinned = claude_cli.verify(root)
    else:
        pinned = codex_cli.verify(root)
    enable_live(root, evidence, pinned, provider)
    status = live_status(root, provider)
    text = ("Live execution is on for Claude: remote jobs on Claude accounts run the pinned Claude Code on "
            "their OpenSwap profile." if provider == "claude" else
            "Live execution is on: remote jobs run with the pinned Codex CLI on the isolated sign-in of their account.")
    _emit(status, args.json, f"{printer.MARK_OK} {text} `openswap worker live disable --provider {provider}` "
                             "turns it off.")
    return 0
