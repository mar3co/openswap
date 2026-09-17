"""Experimental, explicit ChatGPT desktop account switch transaction.

This module deliberately does not claim that relaunch proves the UI identity.
It only performs a guarded file-store transaction and returns an
``awaiting_verification`` result for the caller to verify in Chat, Work, and
Codex.
"""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from openswap.codex.auth import (
    OPENAI_AUTH_CLAIM,
    auth_last_refresh,
    auth_path,
    decode_jwt_claims,
    parse_auth,
)
from openswap.codex.desktop_app import DesktopApp, DesktopAppError
from openswap.codex.engine import CodexEngine
from openswap.exceptions import ClaudeSwitchError
from openswap.models import get_timestamp
from openswap.settings import load_settings


class DesktopSwitchError(ClaudeSwitchError):
    """A desktop switch was refused or could not be completed safely."""


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
        os.replace(tmp, path)
        if os.name != "nt":
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _managed_config_paths(home: Path) -> tuple[Path, ...]:
    username = Path.home().name
    return (
        home / "requirements.toml",
        home / "managed_config.toml",
        Path("/etc/codex/requirements.toml"),
        Path("/etc/codex/managed_config.toml"),
        Path("/etc/codex/config.toml"),
        Path("/Library/Managed Preferences/com.openai.codex.plist"),
        Path("/Library/Managed Preferences") / username / "com.openai.codex.plist",
    )


def _config_check(home: Path) -> dict[str, Any]:
    """Accept only the ordinary local file credential-store arrangement."""
    for managed_path in _managed_config_paths(home):
        if managed_path.exists():
            raise DesktopSwitchError(
                "Desktop switching cannot verify managed Codex requirements; "
                "ask the administrator to confirm the file-store boundary."
            )
    path = home / "config.toml"
    if not path.exists():
        return {"credentialStore": "file (default)", "config": "default"}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise DesktopSwitchError(
            "Cannot safely read the Codex desktop config; fix config.toml and retry."
        ) from exc
    store = data.get("cli_auth_credentials_store")
    if store not in (None, "file"):
        raise DesktopSwitchError(
            "Desktop switching supports only cli_auth_credentials_store='file'; "
            "keyring, auto, and ephemeral stores are refused."
        )
    # Only root and profile scopes can override Codex's own authentication and
    # backend. Nested MCP/plugin commands and sandbox workspace settings are
    # ordinary configuration and must not be mistaken for a custom backend.
    explicit_unsupported = {
        "forced_login_method", "forced_chatgpt_workspace_id",
        "chatgpt_base_url", "openai_base_url", "codex_cli_command",
        "codex_command", "cli_command", "codex_cli_path", "cli_path",
        "backend_command", "remote_backend", "remote_config", "include",
        "config_file",
    }

    def check_scope(scope: object, prefix: str = "") -> list[str]:
        if not isinstance(scope, dict):
            return [prefix.rstrip(".")] if scope is not None else []
        paths: list[str] = []
        for key, value in scope.items():
            lowered = str(key).lower()
            dotted = f"{prefix}{key}"
            if lowered == "cli_auth_credentials_store" and value not in (None, "file"):
                paths.append(dotted)
            elif lowered == "model_provider" and value not in (None, "openai"):
                paths.append(dotted)
            elif lowered == "model_providers" and value:
                paths.append(dotted)
            elif lowered in explicit_unsupported:
                paths.append(dotted)
        return paths

    found = check_scope(data)
    if data.get("profile") is not None:
        found.append("profile")
    profiles = data.get("profiles")
    if profiles is not None:
        if not isinstance(profiles, dict):
            found.append("profiles")
        else:
            for name, profile in profiles.items():
                found.extend(check_scope(profile, f"profiles.{name}."))
    found = sorted(set(found))
    if found:
        raise DesktopSwitchError(
            "Desktop switching is not validated with custom authentication, provider, "
            f"workspace, or remote configuration ({', '.join(found)})."
        )
    return {"credentialStore": "file", "config": "supported"}


def _credential_identity(text: str, *, label: str) -> Any:
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, TypeError, UnicodeError) as exc:
        raise DesktopSwitchError(f"{label} has malformed OAuth credentials.") from exc
    if not isinstance(raw, dict):
        raise DesktopSwitchError(f"{label} has malformed OAuth credentials.")
    tokens = raw.get("tokens")
    if not isinstance(tokens, dict):
        tokens = {}
    if not all(isinstance(tokens.get(k), str) and tokens[k] for k in
               ("id_token", "access_token", "refresh_token")):
        if raw.get("auth_mode") == "apiKey" or raw.get("OPENAI_API_KEY"):
            raise DesktopSwitchError(
                f"{label} uses an API key; desktop switching requires OAuth."
            )
        raise DesktopSwitchError(f"{label} is missing required OAuth token fields.")
    claims = decode_jwt_claims(tokens["id_token"])
    claim_auth = claims.get(OPENAI_AUTH_CLAIM)
    if not isinstance(claim_auth, dict):
        raise DesktopSwitchError(f"{label} has malformed OAuth identity claims.")
    if not isinstance(claims.get("email"), str) or not claims["email"]:
        raise DesktopSwitchError(f"{label} has malformed OAuth identity claims.")
    token_account = tokens.get("account_id")
    claim_account = claim_auth.get("chatgpt_account_id")
    claim_plan = claim_auth.get("chatgpt_plan_type")
    if token_account is not None and not isinstance(token_account, str):
        raise DesktopSwitchError(f"{label} has malformed account identity metadata.")
    if claim_account is not None and not isinstance(claim_account, str):
        raise DesktopSwitchError(f"{label} has malformed account identity metadata.")
    if claim_plan is not None and not isinstance(claim_plan, str):
        raise DesktopSwitchError(f"{label} has malformed account identity metadata.")
    if token_account and claim_account and token_account != claim_account:
        raise DesktopSwitchError(f"{label} has conflicting account identity metadata.")
    try:
        ident = parse_auth(text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise DesktopSwitchError(f"{label} has malformed OAuth identity metadata.") from exc
    if ident is None:
        raise DesktopSwitchError(f"{label} has no usable OAuth credentials.")
    if ident.kind != "oauth":
        raise DesktopSwitchError(f"{label} uses an API key; desktop switching requires OAuth.")
    if not ident.email or not ident.account_id:
        raise DesktopSwitchError(f"{label} is missing stable account identity metadata.")
    return ident


def _optional_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise DesktopSwitchError(f"Cannot safely read transaction input {path.name}.") from exc


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unlink_durable(path: Path) -> None:
    path.unlink(missing_ok=True)
    if os.name != "nt" and path.parent.exists():
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


class DesktopSwitcher:
    """Coordinate a fail-closed desktop restart around a Codex auth swap."""

    def __init__(self, engine: CodexEngine, app: DesktopApp | None = None):
        self.engine = engine
        self.app = app if app is not None else DesktopApp()
        self.recovery_file = engine.state_dir / "desktop-recovery.json"

    def _target_locked(self, number: str) -> tuple[str, dict, str, Any]:
        num, email, account_id = self.engine.resolve_account(str(number))
        roster = self.engine._read_roster()
        rec = self.engine._record(roster, num)
        if rec.get("disabled"):
            raise DesktopSwitchError(f"Account {num} is disabled.")
        if (rec.get("kind") or "oauth") != "oauth":
            raise DesktopSwitchError(f"Account {num} is an API-key account, not OAuth.")
        text = self.engine._slot_text(num)
        ident = _credential_identity(text, label=f"Account {num}")
        if ident.email != email or ident.account_id != account_id:
            raise DesktopSwitchError(
                f"Account {num} credential identity does not match its roster metadata."
            )
        return num, rec, text, ident

    def _read_recovery(self) -> dict:
        text = _optional_text(self.recovery_file)
        if text is None:
            raise DesktopSwitchError("No pending desktop recovery transaction exists.")
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise DesktopSwitchError(
                "The desktop recovery record is malformed. Keep the app stopped and "
                "use a manual sign-in; OpenSwap will not guess at credential recovery."
            ) from exc
        required_strings = (
            "number", "fromNumber", "sourceLive", "sourceFingerprint",
            "targetFingerprint", "rosterBefore", "rosterBeforeFingerprint",
            "rosterWritten", "rosterWrittenFingerprint",
        )
        digest_keys = (
            "sourceFingerprint", "targetFingerprint",
            "rosterBeforeFingerprint", "rosterWrittenFingerprint",
        )
        if (not isinstance(data, dict) or data.get("schemaVersion") != 2
                or not all(isinstance(data.get(k), str) for k in required_strings)
                or not all(len(data.get(k, "")) == 64 and all(
                    char in "0123456789abcdef" for char in data[k]
                ) for k in digest_keys)
                or not all(data[k].isdigit() and int(data[k]) > 0
                           and str(int(data[k])) == data[k]
                           for k in ("number", "fromNumber"))
                or _digest(data["sourceLive"]) != data["sourceFingerprint"]
                or _digest(data["rosterBefore"]) != data["rosterBeforeFingerprint"]
                or _digest(data["rosterWritten"]) != data["rosterWrittenFingerprint"]):
            raise DesktopSwitchError(
                "The desktop recovery record failed integrity validation. Keep the app "
                "stopped and use a manual sign-in; OpenSwap will not overwrite credentials."
            )
        source_ident = _credential_identity(
            data["sourceLive"], label="The recovery source login"
        )
        try:
            roster_before = json.loads(data["rosterBefore"])
            roster_written = json.loads(data["rosterWritten"])
            source_rec = roster_before["accounts"][data["fromNumber"]]
            target_rec = roster_written["accounts"][data["number"]]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise DesktopSwitchError(
                "The desktop recovery roster metadata is malformed. Keep the app "
                "stopped and use a manual sign-in."
            ) from exc
        if (not isinstance(source_rec, dict) or not isinstance(target_rec, dict)
                or source_rec.get("email") != source_ident.email
                or source_rec.get("accountId") != source_ident.account_id
                or str(roster_written.get("activeAccountNumber")) != data["number"]):
            raise DesktopSwitchError(
                "The desktop recovery identities are inconsistent. Keep the app "
                "stopped and use a manual sign-in."
            )
        return data

    def recovery_status(self) -> dict:
        if not self.recovery_file.exists():
            return {"status": "none", "pending": False, "experimental": True}
        data = self._read_recovery()
        return {
            "status": "pending",
            "pending": True,
            "experimental": True,
            "from": {"number": data["fromNumber"]},
            "to": {"number": data["number"]},
            "warning": (
                "A desktop transaction needs recovery. Keep the app idle and use the "
                "explicit recovery action; do not start new work."
            ),
        }

    def recover(
        self, *, confirm_restart: bool = False, confirm_idle: bool = False,
    ) -> dict:
        if not confirm_restart:
            raise DesktopSwitchError("Explicit restart confirmation is required; no changes made.")
        if not confirm_idle:
            raise DesktopSwitchError("Confirm that Chat, Work, and Codex are idle; no changes made.")
        config_path = self.engine.home / "config.toml"
        settings_path = self.engine.backup_dir / "settings.json"
        config_before = _optional_text(config_path)
        settings_before = _optional_text(settings_path)
        _config_check(self.engine.home)
        if load_settings(self.engine.backup_dir).codex_enabled:
            raise DesktopSwitchError(
                "Pause Codex automatic switching first: run "
                "'openswap config set autoswitch.codexEnabled false'."
            )
        with self.engine._lock():
            recovery = self._read_recovery()
            app_info = self.app.preflight(self.engine.home)
            was_running = bool(self.app.is_running())
            if was_running:
                self.app.quit(timeout=20)
            self.app.assert_stopped()

            live = self.engine._live_text()
            live_digest = _digest(live)
            source = recovery["sourceLive"]
            restore_live = False
            if live_digest == recovery["targetFingerprint"]:
                restore_live = True
            elif live_digest == recovery["sourceFingerprint"]:
                pass
            else:
                live_ident = _credential_identity(live, label="The current desktop login")
                source_ident = _credential_identity(source, label="The recovery source login")
                if ((live_ident.email, live_ident.account_id)
                        != (source_ident.email, source_ident.account_id)):
                    raise DesktopSwitchError(
                        "The current login is unrelated to this recovery. The app is stopped; "
                        "use manual sign-in rather than overwriting it."
                    )
                live_time, source_time = auth_last_refresh(live), auth_last_refresh(source)
                if live_time is None or source_time is None or live_time < source_time:
                    raise DesktopSwitchError(
                        "The current login generation cannot be proven newer than recovery. "
                        "The app is stopped; use manual sign-in."
                    )

            roster_now = _optional_text(self.engine.sequence_file) or ""
            if _digest(roster_now) == recovery["rosterWrittenFingerprint"]:
                restore_roster = True
            elif _digest(roster_now) == recovery["rosterBeforeFingerprint"]:
                restore_roster = False
            else:
                raise DesktopSwitchError(
                    "The roster changed outside the failed transaction. It was preserved and "
                    "the app is stopped; resolve accounts before manual sign-in."
                )

            # Inspection is not authority to overwrite later state: compare all
            # mutable inputs again at the last possible point before restore.
            if self.engine._live_text() != live:
                raise DesktopSwitchError(
                    "The live login changed during recovery inspection. The app is stopped; retry."
                )
            if (_optional_text(self.engine.sequence_file) or "") != roster_now:
                raise DesktopSwitchError(
                    "The roster changed during recovery inspection. It was preserved; retry."
                )
            if (_optional_text(config_path) != config_before
                    or _optional_text(settings_path) != settings_before):
                raise DesktopSwitchError(
                    "Desktop configuration or automatic-switch policy changed during recovery; retry."
                )
            _config_check(self.engine.home)
            if load_settings(self.engine.backup_dir).codex_enabled:
                raise DesktopSwitchError(
                    "Pause Codex automatic switching first: run "
                    "'openswap config set autoswitch.codexEnabled false'."
                )

            if restore_live:
                _atomic_text(auth_path(self.engine.home), source)
            if restore_roster:
                if recovery["rosterBefore"]:
                    _atomic_text(self.engine.sequence_file, recovery["rosterBefore"])
                else:
                    self.engine.sequence_file.unlink(missing_ok=True)
            try:
                self.app.launch(self.engine.home, timeout=20)
            except BaseException as exc:
                try:
                    if self.app.is_running():
                        self.app.quit(timeout=20)
                    self.app.assert_stopped()
                except Exception as stop_exc:
                    raise DesktopSwitchError(
                        "Recovery restored the files, but relaunch failed and the app may still "
                        "be running. Keep work idle and retry recovery."
                    ) from stop_exc
                raise DesktopSwitchError(
                    "Recovery restored the files, but relaunch failed. The app is stopped; "
                    "retry the explicit recovery action."
                ) from exc
            _unlink_durable(self.recovery_file)
            return {
                "status": "recovered_awaiting_verification",
                "experimental": True,
                "restored": {"number": recovery["fromNumber"]},
                "app": app_info,
                "warning": (
                    "Verify the restored account in Chat, Work, and Codex before starting work."
                ),
            }

    def preflight(self, number: str) -> dict:
        config = _config_check(self.engine.home)
        if load_settings(self.engine.backup_dir).codex_enabled:
            raise DesktopSwitchError(
                "Pause Codex automatic switching first: run "
                "'openswap config set autoswitch.codexEnabled false'."
            )
        with self.engine._lock():
            num, rec, _target, _ident = self._target_locked(str(number))
            live = self.engine._live_text()
            live_ident = _credential_identity(live, label="The live desktop login")
            current = self.engine._find_slot(live_ident, live)
            if current is None:
                raise DesktopSwitchError(
                    "The live desktop login is unmanaged; add it to OpenSwap before switching."
                )
            app_info = self.app.preflight(self.engine.home)
            return {
                "experimental": True,
                "target": {"number": num, "email": rec.get("email", "")},
                "current": {"number": current, "email": live_ident.email},
                "home": str(self.engine.home),
                "storage": config,
                "app": app_info,
                "running": bool(self.app.is_running()),
                "warning": "Relaunch does not verify the Chat, Work, or Codex UI identity.",
            }

    def switch(
        self, number: str, *, confirm_restart: bool = False,
        confirm_idle: bool = False,
    ) -> dict:
        # Acknowledgements precede even lifecycle inspection, and certainly all
        # side effects. Call preflight separately to present details to users.
        if not confirm_restart:
            raise DesktopSwitchError("Explicit restart confirmation is required; no changes made.")
        if not confirm_idle:
            raise DesktopSwitchError("Confirm that Chat, Work, and Codex are idle; no changes made.")
        config_path = self.engine.home / "config.toml"
        settings_path = self.engine.backup_dir / "settings.json"
        config_before = _optional_text(config_path)
        settings_before = _optional_text(settings_path)
        _config_check(self.engine.home)
        if load_settings(self.engine.backup_dir).codex_enabled:
            raise DesktopSwitchError(
                "Pause Codex automatic switching first: run "
                "'openswap config set autoswitch.codexEnabled false'."
            )

        with self.engine._lock():
            if self.recovery_file.exists():
                raise DesktopSwitchError(
                    "An unresolved desktop recovery record already exists. Keep the app "
                    "idle; run 'openswap codex desktop recovery-status', then use the "
                    "explicit desktop recover command before retrying."
                )
            num, rec, target_before, _target_ident = self._target_locked(str(number))
            roster_path = self.engine.sequence_file
            roster_before = roster_path.read_text(encoding="utf-8") if roster_path.exists() else ""
            live_before = self.engine._live_text()
            live_ident = _credential_identity(live_before, label="The live desktop login")
            from_num = self.engine._find_slot(live_ident, live_before)
            if from_num is None:
                raise DesktopSwitchError(
                    "The live desktop login is unmanaged; add it before switching."
                )
            app_info = self.app.preflight(self.engine.home)
            was_running = bool(self.app.is_running())
            wrote = ""
            roster_written: str | None = None
            lifecycle_stopped = False
            try:
                if was_running:
                    self.app.quit(timeout=20)
                self.app.assert_stopped()
                lifecycle_stopped = True

                # A graceful quit may refresh the outgoing token. Roster and
                # target changes, unlike that expected live refresh, are races.
                roster_now = roster_path.read_text(encoding="utf-8") if roster_path.exists() else ""
                if roster_now != roster_before:
                    raise DesktopSwitchError("The account roster changed during shutdown; retry.")
                target_now = self.engine._slot_text(num)
                if target_now != target_before:
                    raise DesktopSwitchError("The target credentials changed during shutdown; retry.")
                live_after = self.engine._live_text()
                after_ident = _credential_identity(live_after, label="The stopped desktop login")
                if self.engine._find_slot(after_ident, live_after) != from_num:
                    raise DesktopSwitchError("The live login identity changed during shutdown; retry.")
                if (_optional_text(config_path) != config_before
                        or _optional_text(settings_path) != settings_before):
                    raise DesktopSwitchError(
                        "Desktop configuration or automatic-switch policy changed; retry."
                    )
                _config_check(self.engine.home)
                if load_settings(self.engine.backup_dir).codex_enabled:
                    raise DesktopSwitchError(
                        "Codex automatic switching was enabled during shutdown; retry after pausing it."
                    )

                # Preserve a refreshed outgoing generation, but never replace a
                # slot that is demonstrably newer.
                outgoing_slot = self.engine._slot_text(from_num)
                if (live_after != outgoing_slot
                        and not self.engine._slot_is_newer(outgoing_slot, live_after)):
                    _atomic_text(self.engine._slot_auth_path(from_num), live_after)
                target = self.engine._slot_text(num)
                self._target_locked(num)  # validate the selected generation again
                roster_final = (
                    roster_path.read_text(encoding="utf-8") if roster_path.exists() else ""
                )
                if roster_final != roster_before:
                    raise DesktopSwitchError(
                        "The account roster changed before credential write; retry."
                    )
                if self.engine._live_text() != live_after:
                    raise DesktopSwitchError(
                        "The live credentials changed before credential write; retry."
                    )
                if self.engine._slot_text(num) != target:
                    raise DesktopSwitchError(
                        "The selected target generation changed before credential write; retry."
                    )
                if (_optional_text(config_path) != config_before
                        or _optional_text(settings_path) != settings_before):
                    raise DesktopSwitchError(
                        "Desktop configuration or automatic-switch policy changed; retry."
                    )
                _config_check(self.engine.home)
                if load_settings(self.engine.backup_dir).codex_enabled:
                    raise DesktopSwitchError(
                        "Codex automatic switching was enabled during shutdown; retry after pausing it."
                    )

                data = self.engine._read_roster()
                data["activeAccountNumber"] = num
                data["lastUpdated"] = get_timestamp()
                roster_planned = json.dumps(data, indent=2)
                recovery = {
                    "schemaVersion": 2,
                    "number": num,
                    "fromNumber": from_num,
                    "sourceLive": live_after,
                    "sourceFingerprint": _digest(live_after),
                    "targetFingerprint": _digest(target),
                    "rosterBefore": roster_before,
                    "rosterBeforeFingerprint": _digest(roster_before),
                    "rosterWritten": roster_planned,
                    "rosterWrittenFingerprint": _digest(roster_planned),
                }
                _atomic_text(self.recovery_file, json.dumps(recovery, sort_keys=True))
                _atomic_text(auth_path(self.engine.home), target)
                wrote = target
                _atomic_text(roster_path, roster_planned)
                roster_written = _optional_text(roster_path)
                self.app.launch(self.engine.home, timeout=20)
            except BaseException as exc:
                # Roll back only while the live file is still exactly our write;
                # never clobber a login or refresh produced concurrently.
                if wrote:
                    try:
                        if self.app.is_running():
                            self.app.quit(timeout=20)
                        self.app.assert_stopped()
                    except Exception as stop_exc:
                        raise DesktopSwitchError(
                            "Desktop launch failed and the app may still be running. No rollback "
                            "was attempted; stop it, run 'openswap codex desktop recovery-status', "
                            "then use the explicit desktop recover command."
                        ) from stop_exc
                    if self.engine._live_text() != wrote:
                        raise DesktopSwitchError(
                            "Desktop launch failed and auth changed after OpenSwap wrote it. "
                            "The app is stopped, but rollback was withheld to avoid clobbering "
                            "a refreshed login; use the explicit desktop recover command."
                        ) from exc
                    try:
                        _atomic_text(auth_path(self.engine.home), live_after)
                    except Exception as restore_exc:
                        raise DesktopSwitchError(
                            "Desktop switch failed and the stopped app's auth could not be "
                            "restored; use the explicit desktop recover command."
                        ) from restore_exc
                    roster_current = _optional_text(roster_path)
                    if roster_current != roster_written:
                        raise DesktopSwitchError(
                            "Desktop auth was restored, but the roster changed concurrently. "
                            "The concurrent roster was preserved; run desktop recovery-status."
                        ) from exc
                    try:
                        if roster_before:
                            _atomic_text(roster_path, roster_before)
                        else:
                            roster_path.unlink(missing_ok=True)
                        _unlink_durable(self.recovery_file)
                    except Exception as restore_exc:
                        raise DesktopSwitchError(
                            "Desktop auth was restored, but roster rollback was incomplete; "
                            "use the explicit desktop recover command."
                        ) from restore_exc
                if isinstance(exc, ClaudeSwitchError):
                    raise
                state = "is stopped" if lifecycle_stopped else "may still be running"
                raise DesktopSwitchError(
                    f"Desktop switch failed before launch; the app {state}. Credentials "
                    "were not changed."
                ) from exc

            _unlink_durable(self.recovery_file)
            return {
                "status": "awaiting_verification",
                "switched": from_num != num,
                "experimental": True,
                "from": {"number": from_num, "email": live_ident.email},
                "to": {"number": num, "email": rec.get("email", "")},
                "app": app_info,
                "warning": (
                    "Open the profile menu and verify the intended account in Chat, "
                    "Work, and Codex before starting work."
                ),
            }


__all__ = ["DesktopApp", "DesktopAppError", "DesktopSwitcher", "DesktopSwitchError"]
