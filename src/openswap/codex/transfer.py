"""Export and import Codex ``auth.json`` envelopes.

A separate format from Claude ``openswap.transfer.FORMAT_VERSION``: Claude
import requires ``config.oauthAccount``, which Codex slots do not have.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openswap import __version__
from openswap.codex.auth import parse_auth
from openswap.exceptions import AccountNotFoundError, TransferError
from openswap.fsutil import replace_with_retry
from openswap.models import Platform, get_timestamp, normalize_alias

if TYPE_CHECKING:
    from openswap.codex.engine import CodexEngine

FORMAT_VERSION = 1
PROVIDER = "codex"

_PLATFORM_TAG = {
    Platform.MACOS: "macos",
    Platform.LINUX: "linux",
    Platform.WSL: "wsl",
    Platform.WINDOWS: "windows",
    Platform.UNKNOWN: "unknown",
}


def _eprint(msg: str) -> None:
    """Print to stderr so stdout stays pure JSON in pipe mode."""
    print(msg, file=sys.stderr)


def _parse_payload(text: str, label: str) -> dict:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TransferError(f"{label} is not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        raise TransferError(f"{label} must be a JSON object")
    return parsed


def _atomic_write_file(path: Path, content: str) -> None:
    """Write text atomically with 0600 perms (see Claude transfer.py)."""
    if path.is_dir():
        raise TransferError(
            f"export destination must be a file path, not a directory: {path}"
        )
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        fd = -1
        replace_with_retry(tmp_path, str(path))
        if sys.platform != "win32":
            os.chmod(str(path), 0o600)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _credential_identity(auth: dict, parsed_auth: Any) -> tuple[str, ...]:
    """Return the semantic login identity, independent of JSON serialization."""
    if parsed_auth.kind == "api_key":
        api_key = auth.get("OPENAI_API_KEY")
        if not isinstance(api_key, str) or not api_key:
            raise TransferError("API-key auth is missing OPENAI_API_KEY")
        return ("api_key", api_key)
    return ("oauth", parsed_auth.email, parsed_auth.account_id)


def _validate_imported_account(account: dict) -> tuple[str, str, tuple[str, ...], Any]:
    if not isinstance(account, dict):
        raise TransferError("account entry must be a JSON object")

    email = account.get("email")
    if not isinstance(email, str):
        raise TransferError(f"invalid or missing email in imported account: {email!r}")

    raw_number = account.get("number")
    if isinstance(raw_number, bool) or not isinstance(raw_number, int) or raw_number < 1:
        raise TransferError(
            f"invalid slot number in imported account ({email}): {raw_number!r}"
        )

    for field in ("accountId", "planType", "kind", "added", "alias"):
        if field in account and account[field] is not None:
            if not isinstance(account[field], str):
                raise TransferError(
                    f"{field} for {email} must be a string, got {type(account[field]).__name__}"
                )

    alias = account.get("alias")
    if isinstance(alias, str):
        try:
            normalize_alias(alias)
        except ValueError as e:
            raise TransferError(f"invalid alias for {email}: {e}") from e

    auth = account.get("auth")
    if not isinstance(auth, dict):
        raise TransferError(f"auth for {email} must be a JSON object")
    parsed_auth = parse_auth(json.dumps(auth))
    if parsed_auth is None:
        raise TransferError(f"auth for {email} is not a usable Codex login")

    metadata_checks = {
        "email": parsed_auth.email,
        "accountId": parsed_auth.account_id,
        "kind": parsed_auth.kind,
    }
    for field, parsed_value in metadata_checks.items():
        if field in account and (account.get(field) or "") != parsed_value:
            raise TransferError(
                f"{field} for imported account {email or raw_number} does not match auth"
            )

    return (
        parsed_auth.email,
        str(raw_number),
        _credential_identity(auth, parsed_auth),
        parsed_auth,
    )


def _find_existing_slot(
    engine: CodexEngine,
    data: dict,
    email: str,
    account_id: str,
    credential_identity: tuple[str, ...],
) -> str | None:
    for num, rec in (data.get("accounts") or {}).items():
        if credential_identity[0] == "oauth":
            matches = (
                rec.get("email") == email
                and (rec.get("accountId") or "") == account_id
            )
        else:
            slot_text = engine._slot_text(str(num))
            parsed = parse_auth(slot_text) if slot_text else None
            if parsed is None or parsed.kind != "api_key":
                matches = False
            else:
                try:
                    auth = _parse_payload(slot_text, f"auth.json for slot {num}")
                    matches = _credential_identity(auth, parsed) == credential_identity
                except TransferError:
                    matches = False
        if matches:
            return str(num)
    return None


def _account_record(entry: dict[str, Any]) -> dict[str, Any]:
    record = {
        "email": entry["email"],
        "accountId": entry["account_id"],
        "planType": entry["plan_type"],
        "kind": entry["kind"],
        "added": entry["added"],
    }
    if entry.get("alias"):
        record["alias"] = entry["alias"]
    if entry.get("disabled"):
        record["disabled"] = True
    return record


def _ensure_sequence(data: dict, num: str) -> None:
    seq = list(data.get("sequence") or [])
    if num not in {str(n) for n in seq}:
        seq.append(int(num) if num.isdigit() else num)
    data["sequence"] = sorted(
        seq, key=lambda n: int(n) if str(n).isdigit() else 0
    )


def export_accounts(
    engine: CodexEngine,
    destination: str,
    account: str | None = None,
) -> None:
    """Export Codex slots to a JSON file or stdout."""
    data = engine._read_roster()
    accounts_map = data.get("accounts") or {}
    if not accounts_map:
        raise TransferError("no accounts to export — run openswap codex add first")

    explicit_account = account is not None
    if explicit_account:
        try:
            resolved, _email, _acc = engine.resolve_account(account)
        except AccountNotFoundError:
            raise TransferError(f"account not found: {account}") from None
        if resolved not in accounts_map:
            raise TransferError(f"account not found: {account}")
        target_nums = [resolved]
    else:
        target_nums = engine._seq_nums(data) or sorted(accounts_map.keys(), key=lambda n: int(n) if str(n).isdigit() else 0)

    live_slot = engine.current_account_number()
    accounts_payload: list[dict[str, Any]] = []
    for num in target_nums:
        record = accounts_map.get(str(num)) or {}
        email = record.get("email", "") or ""
        if live_slot == str(num):
            text = engine._live_text() or engine._slot_text(num)
        else:
            text = engine._slot_text(num)
        if not text:
            if explicit_account:
                raise TransferError(
                    f"no stored auth.json for account {num} ({email})"
                )
            _eprint(
                f"Skipping Account-{num} ({email}): no stored auth.json"
            )
            continue
        auth_obj = _parse_payload(text, f"auth.json for {email or num}")
        entry: dict[str, Any] = {
            "number": int(num) if str(num).isdigit() else num,
            "email": email,
            "accountId": record.get("accountId", "") or "",
            "planType": record.get("planType", "") or "",
            "kind": record.get("kind", "") or "oauth",
            "added": record.get("added", "") or "",
            "disabled": bool(record.get("disabled")),
            "auth": auth_obj,
        }
        if record.get("alias"):
            entry["alias"] = record["alias"]
        accounts_payload.append(entry)

    if not accounts_payload:
        raise TransferError(
            "no exportable accounts — all managed slots are missing stored auth.json"
        )

    recorded_active = data.get("activeAccountNumber")
    exported_nums = {str(a["number"]) for a in accounts_payload}
    if recorded_active is None:
        active_in_payload = None
    else:
        recorded_str = str(recorded_active)
        active_in_payload = recorded_str if recorded_str in exported_nums else None

    envelope = {
        "version": FORMAT_VERSION,
        "provider": PROVIDER,
        "exportedAt": get_timestamp(),
        "exportedFrom": _PLATFORM_TAG.get(Platform.detect(), "unknown"),
        "swapVersion": __version__,
        "encrypted": False,
        "activeAccountNumber": active_in_payload,
        "accounts": accounts_payload,
    }
    serialized = json.dumps(envelope, indent=2)

    if destination == "-":
        sys.stdout.write(serialized)
        sys.stdout.write("\n")
        sys.stdout.flush()
        return

    out_path = Path(destination).expanduser()
    _atomic_write_file(out_path, serialized + "\n")
    _eprint(f"Exported {len(accounts_payload)} account(s) to {out_path}")


def import_accounts(
    engine: CodexEngine,
    source: str,
    force: bool = False,
) -> None:
    """Import Codex slots from a JSON file or stdin. Does not switch live."""
    if source == "-":
        text = sys.stdin.read()
    else:
        in_path = Path(source).expanduser()
        if not in_path.exists():
            raise TransferError(f"import file not found: {in_path}")
        text = in_path.read_text(encoding="utf-8")

    try:
        envelope = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TransferError(f"export file is not valid JSON: {exc}")

    if not isinstance(envelope, dict):
        raise TransferError("export file must be a JSON object")

    if envelope.get("provider") != PROVIDER:
        raise TransferError("not a Codex export (missing or wrong provider)")

    version = envelope.get("version")
    if version != FORMAT_VERSION:
        raise TransferError(
            f"unsupported export version: {version!r} (expected {FORMAT_VERSION})"
        )

    if envelope.get("encrypted") is True:
        raise TransferError(
            "encrypted exports are not supported in this version — "
            "decrypt before piping (e.g. gpg -d backup.gpg | openswap codex import -)"
        )

    accounts = envelope.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise TransferError("export file has no accounts to import")

    local_data = engine._read_roster()
    local_aliases: dict[str, tuple[str, ...]] = {}
    for num, acc in (local_data.get("accounts") or {}).items():
        alias = acc.get("alias")
        if not alias:
            continue
        owner: tuple[str, ...] = (
            "oauth",
            acc.get("email", ""),
            acc.get("accountId", "") or "",
        )
        slot_text = engine._slot_text(str(num))
        parsed = parse_auth(slot_text) if slot_text else None
        if parsed is not None:
            try:
                owner = _credential_identity(
                    _parse_payload(slot_text, f"auth.json for slot {num}"), parsed
                )
            except TransferError:
                pass
        local_aliases[alias.lower()] = owner
    normalized: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, ...]] = set()
    seen_aliases: set[str] = set()
    for raw in accounts:
        email, exported_num, credential_identity, parsed_auth = (
            _validate_imported_account(raw)
        )
        account_id = parsed_auth.account_id
        key = credential_identity
        if key in seen_keys:
            raise TransferError(
                f"duplicate account in export: {email} (accountId={account_id or 'none'})"
            )
        seen_keys.add(key)

        alias = raw.get("alias") or None
        if alias:
            alias_key = normalize_alias(alias)
            if alias_key in seen_aliases:
                raise TransferError(f"duplicate alias in export: {alias_key}")
            seen_aliases.add(alias_key)
            owner = local_aliases.get(alias_key)
            if owner is not None and owner != credential_identity:
                _eprint(
                    f"Warning: alias '{alias_key}' for {email} already used by an "
                    "existing account, dropping the imported alias"
                )
                alias = None
            else:
                alias = alias_key

        normalized.append(
            {
                "email": email,
                "exported_num": exported_num,
                "account_id": account_id,
                "plan_type": parsed_auth.plan_type,
                "kind": parsed_auth.kind,
                "added": raw.get("added") or get_timestamp(),
                "alias": alias,
                "disabled": bool(raw.get("disabled")),
                "auth_text": json.dumps(raw["auth"]),
                "credential_identity": credential_identity,
            }
        )

    imported = 0
    skipped = 0
    overwritten = 0
    dirty = False
    envelope_active = envelope.get("activeAccountNumber")
    envelope_active_str = (
        str(envelope_active) if envelope_active not in (None, "") else None
    )
    resolved_active_slot: str | None = None

    with engine._lock():
        data = dict(engine._read_roster())
        accounts_map = dict(data.get("accounts") or {})
        data["accounts"] = accounts_map
        data["sequence"] = list(data.get("sequence") or [])
        try:
            for entry in normalized:
                is_envelope_active = (
                    envelope_active_str is not None
                    and entry["exported_num"] == envelope_active_str
                )
                existing_slot = _find_existing_slot(
                    engine,
                    data,
                    entry["email"],
                    entry["account_id"],
                    entry["credential_identity"],
                )
                if existing_slot is not None:
                    if not force:
                        _eprint(
                            f"Skipped {entry['email']} (already exists, use --force)"
                        )
                        skipped += 1
                        if is_envelope_active:
                            resolved_active_slot = existing_slot
                        continue
                    target_num = existing_slot
                    outcome = "overwrote"
                else:
                    if entry["exported_num"] not in accounts_map:
                        target_num = entry["exported_num"]
                    else:
                        target_num = engine._next_free_number(data)
                    outcome = "imported"

                engine._write_slot(target_num, entry["auth_text"])
                accounts_map[target_num] = _account_record(entry)
                _ensure_sequence(data, target_num)
                dirty = True

                if is_envelope_active:
                    resolved_active_slot = target_num

                if outcome == "overwrote":
                    _eprint(f"Overwrote {entry['email']} (slot {target_num})")
                    overwritten += 1
                else:
                    _eprint(f"Imported {entry['email']} → slot {target_num}")
                    imported += 1

            if (
                data.get("activeAccountNumber") in (None, 0, "")
                and resolved_active_slot is not None
            ):
                data["activeAccountNumber"] = str(resolved_active_slot)
                dirty = True

            if dirty:
                data["accounts"] = accounts_map
                engine._write_roster(data)
        except Exception:
            if dirty:
                try:
                    data["accounts"] = accounts_map
                    engine._write_roster(data)
                except Exception:
                    pass
            raise

    _eprint(
        f"Done: {imported} imported, {overwritten} overwritten, {skipped} skipped"
    )
