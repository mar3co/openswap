"""Codex CLI account engine: roster, slot homes, switch, and usage snapshot."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable

from openswap.codex.auth import (
    CodexIdentity,
    auth_fingerprint,
    auth_last_refresh,
    auth_path,
    codex_home,
    parse_auth,
)
from openswap.codex.usage import rate_limits_to_usage, read_rate_limits
from openswap.exceptions import (
    AccountNotFoundError,
    ClaudeSwitchError,
    ConfigError,
    ValidationError,
)
from openswap.json_output import SCHEMA_VERSION, USAGE_API_KEY, USAGE_NO_CREDENTIALS, account_row
from openswap.locking import FileLock
from openswap.logging_config import setup_logging
from openswap.models import (
    AccountSnapshot,
    AccountsSnapshot,
    get_timestamp,
    normalize_alias,
)
from openswap import oauth, paths
from openswap.printer import accent, bolded, dimmed, muted
from openswap.settings import atomic_write_json
from openswap.usage_store import FetchRecord, UsageEntry, UsageStore, with_sentinel
from openswap.engine.notes import _usage_entry_lines


class CodexAuthError(ClaudeSwitchError):
    """No live Codex login, or the login is already a managed slot."""


class CodexSwitchError(ClaudeSwitchError):
    """Switch refused (unmanaged live login, missing target, …)."""


_EMPTY_ROSTER = {
    "schemaVersion": 1,
    "activeAccountNumber": None,
    "lastUpdated": "",
    "sequence": [],
    "accounts": {},
}


class CodexEngine:
    provider = "codex"

    def __init__(
        self,
        *,
        backup_dir: Path | None = None,
        home: Path | None = None,
        codex_bin: Callable[[], str | None] | None = None,
        read_limits: Callable[..., dict] | None = None,
        clock: Callable[[], float] = time.time,
        debug: bool = False,
    ):
        self.backup_dir = backup_dir if backup_dir is not None else paths.get_backup_root()
        self.home = home if home is not None else codex_home()
        self.state_dir = self.backup_dir / "codex"
        self.sequence_file = self.state_dir / "sequence.json"
        self.lock_file = self.state_dir / ".lock"
        self.slots_dir = self.state_dir / "slots"
        self.clock = clock
        self._usage_store = UsageStore(self.state_dir / "cache", clock)
        self._logger = setup_logging(self.backup_dir, debug=debug)
        self._codex_bin = codex_bin if codex_bin is not None else (lambda: shutil.which("codex"))
        self._read_limits = read_limits if read_limits is not None else read_rate_limits
        self._poll_inputs: tuple[float, tuple[str, ...]] | None = None
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.state_dir, 0o700)

    # -- roster / files -------------------------------------------------------

    def _lock(self) -> FileLock:
        return FileLock(self.lock_file)

    def _read_roster(self) -> dict:
        if not self.sequence_file.exists():
            data = dict(_EMPTY_ROSTER)
            data["lastUpdated"] = get_timestamp()
            return data
        try:
            raw = json.loads(self.sequence_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            data = dict(_EMPTY_ROSTER)
            data["lastUpdated"] = get_timestamp()
            return data
        return raw if isinstance(raw, dict) else dict(_EMPTY_ROSTER)

    def _write_roster(self, data: dict) -> None:
        data = dict(data)
        data["lastUpdated"] = get_timestamp()
        atomic_write_json(self.sequence_file, data)

    def _slot_dir(self, num: str) -> Path:
        return self.slots_dir / str(num)

    def _slot_auth_path(self, num: str) -> Path:
        return self._slot_dir(num) / "auth.json"

    def _write_auth_file(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(path.parent, 0o700)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            os.write(fd, text.encode("utf-8"))
            os.close(fd)
            fd = -1
            if os.name != "nt":
                os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _write_slot(self, num: str, text: str) -> None:
        self._write_auth_file(self._slot_auth_path(num), text)

    def _write_live(self, text: str) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.home, 0o700)
        self._write_auth_file(auth_path(self.home), text)

    def _live_text(self) -> str:
        path = auth_path(self.home)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def _slot_text(self, num: str) -> str:
        try:
            return self._slot_auth_path(num).read_text(encoding="utf-8")
        except OSError:
            return ""

    def _seq_nums(self, data: dict) -> list[str]:
        out: list[str] = []
        for item in data.get("sequence") or []:
            text = str(item)
            if text not in out:
                out.append(text)
        return out

    def _next_free_number(self, data: dict) -> str:
        taken = {int(n) for n in self._seq_nums(data) if str(n).isdigit()}
        n = 1
        while n in taken:
            n += 1
        return str(n)

    def _record(self, data: dict, num: str) -> dict:
        return (data.get("accounts") or {}).get(str(num)) or {}

    def _ref(self, num: str | None, email: str = "") -> dict | None:
        if num is None:
            return None
        return {"number": str(num), "email": email}

    def _find_slot(self, identity: CodexIdentity | None, text: str) -> str | None:
        data = self._read_roster()
        accounts = data.get("accounts") or {}
        if identity is not None and (identity.email or identity.account_id):
            for num, rec in accounts.items():
                if rec.get("email") == identity.email and rec.get("accountId") == identity.account_id:
                    return str(num)
            # Distinct ChatGPT account (email/accountId present and unmatched).
            # Do not fall through to refresh-token fingerprint — tests (and
            # rotated tokens) can share a refresh string across identities.
            return None
        live_fp = auth_fingerprint(text)
        if live_fp:
            for num in self._seq_nums(data):
                slot_text = self._slot_text(num)
                if slot_text and auth_fingerprint(slot_text) == live_fp:
                    return str(num)
        return None

    def _live_slot(self, live: str | None = None) -> str | None:
        text = self._live_text() if live is None else live
        ident = parse_auth(text)
        if ident is None:
            return None
        return self._find_slot(ident, text)

    def _slot_is_newer(self, slot: str, live: str) -> bool:
        slot_ts = auth_last_refresh(slot)
        live_ts = auth_last_refresh(live)
        if slot_ts is None or live_ts is None:
            return False
        try:
            return slot_ts > live_ts
        except TypeError:
            return False

    def _write_slot_from_live(self, num: str, live: str) -> None:
        slot = self._slot_text(num)
        if live == slot or self._slot_is_newer(slot, live):
            return
        self._write_slot(num, live)

    def _capture_live(self, num: str) -> bool:
        """Copy live ``auth.json`` into ``num`` only when live still is that slot.

        Returns True when the live login matches ``num``. False if unmanaged,
        a different slot, or the roster lock is busy. Skips the write when
        the slot already holds a newer ``last_refresh`` generation.
        """
        lock = self._lock()
        if not lock.acquire():
            return False
        try:
            live = self._live_text()
            if self._live_slot(live) != str(num):
                return False
            self._write_slot_from_live(num, live)
            return True
        finally:
            lock.release()

    # -- identity / roster lookups -------------------------------------------

    def resolve_account(self, identifier: str) -> tuple[str, str, str]:
        text = str(identifier)
        data = self._read_roster()
        accounts = data.get("accounts") or {}
        if text.isdigit() and text in accounts:
            rec = accounts[text]
            return text, rec.get("email", ""), rec.get("accountId", "") or ""
        alias_hits = [
            num for num, rec in accounts.items() if rec.get("alias") == text
        ]
        if len(alias_hits) == 1:
            rec = accounts[alias_hits[0]]
            return alias_hits[0], rec.get("email", ""), rec.get("accountId", "") or ""
        email_hits = [
            num for num, rec in accounts.items() if rec.get("email") == text
        ]
        if len(email_hits) == 1:
            rec = accounts[email_hits[0]]
            return email_hits[0], rec.get("email", ""), rec.get("accountId", "") or ""
        if len(email_hits) > 1:
            details = ", ".join(email_hits)
            raise ConfigError(
                f"Email '{identifier}' is ambiguous — matches accounts: {details}."
            )
        raise AccountNotFoundError(f"No account found with identifier: {identifier}")

    def current_account_number(self) -> str | None:
        """Slot of the live login; ``None`` when there is none or it's unmanaged."""
        return self._live_slot()

    def has_live_login(self) -> bool:
        return parse_auth(self._live_text()) is not None

    def live_identity(self) -> tuple[str, str] | None:
        ident = parse_auth(self._live_text())
        if ident is None:
            return None
        return ident.email, ident.account_id

    def slot_identity(self, num: str | int) -> tuple[str, str] | None:
        rec = self._record(self._read_roster(), str(num))
        if not rec:
            return None
        return rec.get("email", "") or "", rec.get("accountId", "") or ""

    def account_email(self, account_num: str) -> str:
        return self._record(self._read_roster(), account_num).get("email", "") or ""

    def account_kind_for(self, account_num: str) -> str:
        return self._record(self._read_roster(), account_num).get("kind", "") or "oauth"

    def read_account_credentials(self, account_num: str, email: str) -> str:
        return self._slot_text(account_num)

    def switchable_account_numbers(self) -> list[str]:
        data = self._read_roster()
        out: list[str] = []
        for num in self._seq_nums(data):
            rec = self._record(data, num)
            if rec.get("disabled"):
                continue
            text = self._slot_text(num)
            if parse_auth(text) is None:
                continue
            out.append(num)
        return out

    def freshen_backup(self, number: str, email: str) -> str:
        return "ok"

    def set_poll_policy_inputs(self, threshold: float, models: tuple[str, ...]) -> None:
        self._poll_inputs = (threshold, models)

    # -- mutations ------------------------------------------------------------

    def add_account(self, alias: str | None = None) -> str:
        live = self._live_text()
        ident = parse_auth(live)
        if ident is None:
            raise CodexAuthError(
                f"No Codex login found at {auth_path(self.home)}. Run 'codex login' first."
            )
        with self._lock():
            data = self._read_roster()
            existing = self._find_slot(ident, live)
            if existing is not None:
                email = ident.email or existing
                raise CodexAuthError(f"{email} is already account {existing}")
            num = self._next_free_number(data)
            self._write_slot(num, live)
            rec = {
                "email": ident.email,
                "accountId": ident.account_id,
                "planType": ident.plan_type,
                "kind": ident.kind,
                "added": get_timestamp(),
            }
            if alias:
                rec["alias"] = normalize_alias(alias)
            accounts = dict(data.get("accounts") or {})
            accounts[num] = rec
            seq = list(data.get("sequence") or [])
            seq_int = int(num) if num.isdigit() else num
            if seq_int not in seq and num not in seq:
                seq.append(seq_int if isinstance(seq_int, int) else num)
            data["accounts"] = accounts
            data["sequence"] = seq
            data["activeAccountNumber"] = num
            self._write_roster(data)
            return num

    def set_account_disabled(self, identifier: str, disabled: bool) -> None:
        with self._lock():
            num, _email, _acc = self.resolve_account(identifier)
            data = self._read_roster()
            rec = self._record(data, num)
            if not rec:
                raise AccountNotFoundError(f"Account-{num} does not exist")
            if disabled:
                rec["disabled"] = True
            else:
                rec.pop("disabled", None)
            data.setdefault("accounts", {})[num] = rec
            self._write_roster(data)

    def set_alias(self, identifier: str, alias: str) -> tuple[str, str]:
        try:
            normalized = normalize_alias(alias)
        except ValueError as e:
            raise ValidationError(str(e)) from e
        with self._lock():
            num, _email, _acc = self.resolve_account(identifier)
            data = self._read_roster()
            rec = self._record(data, num)
            if not rec:
                raise AccountNotFoundError(f"Account-{num} does not exist")
            for other, other_rec in (data.get("accounts") or {}).items():
                if other != num and other_rec.get("alias") == normalized:
                    raise ConfigError(
                        f"Alias '{normalized}' is already used by account {other}"
                    )
            rec["alias"] = normalized
            data.setdefault("accounts", {})[num] = rec
            self._write_roster(data)
            return num, normalized

    def unset_alias(self, identifier: str) -> str:
        with self._lock():
            num, _email, _acc = self.resolve_account(identifier)
            data = self._read_roster()
            rec = self._record(data, num)
            if rec and "alias" in rec:
                del rec["alias"]
                data.setdefault("accounts", {})[num] = rec
                self._write_roster(data)
            return num

    def list_aliases(self) -> list[tuple[str, str, str]]:
        data = self._read_roster()
        rows = [
            (num, rec.get("alias"), rec.get("email", ""))
            for num, rec in (data.get("accounts") or {}).items()
            if rec.get("alias")
        ]
        return sorted(rows, key=lambda r: int(r[0]) if str(r[0]).isdigit() else 0)

    def remove_account(self, identifier: str, assume_yes: bool = False) -> None:
        num, email, _acc = self.resolve_account(identifier)
        if not assume_yes:
            confirm = input(
                f"Are you sure you want to permanently remove "
                f"Account-{num} ({email})? [y/N] "
            )
            if confirm.lower() != "y":
                print(dimmed("Cancelled"))
                return
        with self._lock():
            num, email, _acc = self.resolve_account(identifier)
            data = self._read_roster()
            accounts = dict(data.get("accounts") or {})
            accounts.pop(str(num), None)
            seq = [n for n in (data.get("sequence") or []) if str(n) != str(num)]
            data["accounts"] = accounts
            data["sequence"] = seq
            if str(data.get("activeAccountNumber")) == str(num):
                data["activeAccountNumber"] = None
            self._write_roster(data)
            slot_dir = self._slot_dir(num)
            if slot_dir.exists():
                shutil.rmtree(slot_dir)
        print(f"{accent('Removed')} Account-{num} ({email})")

    def _map_sequence(self, data: dict, mapping: dict[str, str]) -> None:
        seq: list = []
        seen: set[str] = set()
        for n in data.get("sequence") or []:
            text = mapping.get(str(n), str(n))
            if text in seen:
                continue
            seen.add(text)
            seq.append(int(text) if text.isdigit() else text)
        seq.sort(key=lambda n: n if isinstance(n, int) else 0)
        data["sequence"] = seq

    def _map_active(self, data: dict, mapping: dict[str, str]) -> None:
        active = data.get("activeAccountNumber")
        if active is None:
            return
        key = str(active)
        if key in mapping:
            data["activeAccountNumber"] = mapping[key]

    def _swap_slot_dirs(self, num_a: str, num_b: str) -> None:
        dir_a = self._slot_dir(num_a)
        dir_b = self._slot_dir(num_b)
        self.slots_dir.mkdir(parents=True, exist_ok=True)
        leftovers = sorted(self.slots_dir.glob(".swapping-*"))
        if leftovers:
            raise ConfigError(
                f"Found leftover slot swap staging: {leftovers[0]}. "
                "Verify both accounts, then delete the directory and retry."
            )
        a_exists = dir_a.exists()
        b_exists = dir_b.exists()
        staging = None
        try:
            if a_exists and b_exists:
                staging = self.slots_dir / f".swapping-{num_a}"
                os.replace(dir_a, staging)
                os.replace(dir_b, dir_a)
                os.replace(staging, dir_b)
                staging = None
            elif a_exists:
                os.replace(dir_a, dir_b)
            elif b_exists:
                os.replace(dir_b, dir_a)
        finally:
            if staging is not None and staging.exists():
                try:
                    if not dir_a.exists():
                        os.replace(staging, dir_a)
                    elif not dir_b.exists():
                        os.replace(dir_a, dir_b)
                        os.replace(staging, dir_a)
                except OSError:
                    pass

    def _move_slot_dir(self, src: str, dest: str) -> None:
        src_dir = self._slot_dir(src)
        dest_dir = self._slot_dir(dest)
        self.slots_dir.mkdir(parents=True, exist_ok=True)
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        if src_dir.exists():
            os.replace(src_dir, dest_dir)

    def swap_accounts(self, first: str, second: str) -> tuple[str, str]:
        """Exchange two Codex accounts' slot numbers under the roster lock."""
        with self._lock():
            return self._swap_accounts_locked(first, second)

    def _swap_accounts_locked(self, first: str, second: str) -> tuple[str, str]:
        num_a, email_a, _acc_a = self.resolve_account(first)
        num_b, email_b, _acc_b = self.resolve_account(second)
        if num_a == num_b:
            raise ValidationError("Cannot swap an account with itself")
        data = dict(self._read_roster())
        accounts = dict(data.get("accounts") or {})
        record_a = accounts.get(str(num_a))
        record_b = accounts.get(str(num_b))
        if not record_a:
            raise AccountNotFoundError(f"Account-{num_a} does not exist")
        if not record_b:
            raise AccountNotFoundError(f"Account-{num_b} does not exist")
        dirs_swapped = False
        try:
            self._swap_slot_dirs(num_a, num_b)
            dirs_swapped = True
            accounts[str(num_a)], accounts[str(num_b)] = record_b, record_a
            data["accounts"] = accounts
            mapping = {str(num_a): str(num_b), str(num_b): str(num_a)}
            self._map_sequence(data, mapping)
            self._map_active(data, mapping)
            self._write_roster(data)
        except BaseException:
            if dirs_swapped:
                try:
                    self._swap_slot_dirs(num_a, num_b)
                except Exception:
                    pass
            raise
        self._logger.info(
            "Swapped Codex slots: %s (%s) <-> %s (%s)", num_a, email_a, num_b, email_b
        )
        return num_a, num_b

    def move_account(self, account: str, slot: str) -> tuple[str, str, bool]:
        """Assign ``account`` to slot ``slot``; occupied target swaps."""
        target = str(slot).strip()
        if not target.isdigit() or int(target) < 1:
            raise ValidationError(
                f"Target slot must be a positive slot number, got: {target!r} "
                "(use `swap` to trade two accounts by identifier)"
            )
        target = str(int(target))
        with self._lock():
            num_src, _email, _acc = self.resolve_account(account)
            data = self._read_roster()
            accounts = data.get("accounts") or {}
            if not accounts.get(num_src):
                raise AccountNotFoundError(f"Account-{num_src} does not exist")
            max_slot = max(
                (int(n) for n in accounts if str(n).isdigit()), default=0
            )
            cap = max(99, max_slot)
            if int(target) > cap:
                raise ValidationError(
                    f"Target slot {target} is out of range (1-{cap}): new accounts "
                    "are numbered from the highest slot, so a large target would "
                    "inflate future account numbers"
                )
            if num_src == target:
                return num_src, target, False
            if accounts.get(target):
                self._swap_accounts_locked(num_src, target)
                return num_src, target, True
            self._relocate_locked(num_src, target)
            return num_src, target, False

    def _relocate_locked(self, num_src: str, target: str) -> None:
        data = dict(self._read_roster())
        accounts = dict(data.get("accounts") or {})
        record = accounts.get(str(num_src))
        if not record:
            raise AccountNotFoundError(f"Account-{num_src} does not exist")
        if accounts.get(str(target)):
            raise ValidationError(
                f"Slot {target} is already occupied — retry the move"
            )
        email = record.get("email", "")
        dirs_moved = False
        try:
            self._move_slot_dir(num_src, target)
            dirs_moved = True
            accounts[str(target)] = record
            del accounts[str(num_src)]
            data["accounts"] = accounts
            self._map_sequence(data, {str(num_src): str(target)})
            self._map_active(data, {str(num_src): str(target)})
            self._write_roster(data)
        except BaseException:
            if dirs_moved:
                try:
                    self._move_slot_dir(target, num_src)
                except Exception:
                    pass
            raise
        self._logger.info("Moved Codex slot: %s (%s) -> %s", num_src, email, target)

    def switch_to(
        self, identifier: str, json_output: bool = False, force: bool = False
    ) -> dict | None:
        with self._lock():
            num, email, _acc = self.resolve_account(identifier)
            target_text = self._slot_text(num)
            if parse_auth(target_text) is None:
                raise CodexSwitchError(f"Account {num} has no usable Codex login.")
            live = self._live_text()
            live_ident = parse_auth(live)
            from_num = None
            from_email = ""
            if live_ident is not None:
                matched = self._find_slot(live_ident, live)
                if matched is not None:
                    self._write_slot_from_live(matched, live)
                    from_num = matched
                    from_email = live_ident.email
                elif not force:
                    shown = live_ident.email or "unknown"
                    raise CodexSwitchError(
                        f"The live Codex login ({shown}) is not managed. Run "
                        "'openswap codex add' first, or pass --force to overwrite it."
                    )
            data = self._read_roster()
            if from_num == str(num):
                if str(data.get("activeAccountNumber") or "") != str(num):
                    data["activeAccountNumber"] = str(num)
                    self._write_roster(data)
                result = {
                    "switched": False,
                    "from": self._ref(str(num), email),
                    "to": self._ref(str(num), email),
                    "reason": "already-active",
                    "warnings": [],
                }
                return result
            self._write_live(target_text)
            data["activeAccountNumber"] = str(num)
            self._write_roster(data)
            return {
                "switched": True,
                "from": self._ref(from_num, from_email),
                "to": self._ref(str(num), email),
                "reason": "switched",
                "warnings": [],
            }

    def switch(
        self, strategy: str | None = None, json_output: bool = False, force: bool = False
    ) -> dict | None:
        if strategy not in (None, "best", "next-available"):
            raise ValueError(f"unknown switch strategy: {strategy!r}")
        nums = self.switchable_account_numbers()
        if not nums:
            raise CodexSwitchError("No switchable Codex accounts.")
        active = self.current_account_number()
        if strategy is None:
            target = self._next_after(active, nums)
        elif strategy == "best":
            target = self._best_slot(nums) or active or nums[0]
        else:
            target = self._next_available(active, nums)
        return self.switch_to(target, json_output=json_output, force=force)

    def _next_after(self, active: str | None, nums: list[str]) -> str:
        data = self._read_roster()
        order = [n for n in self._seq_nums(data) if n in set(nums)]
        if not order:
            return nums[0]
        if active in order:
            idx = order.index(active)
            return order[(idx + 1) % len(order)]
        return order[0]

    def _best_slot(self, nums: list[str]) -> str | None:
        entries = self.usage_entries_by_account(fetch=None)
        best: str | None = None
        best_hr: float | None = None
        for num in sorted(nums, key=lambda n: int(n) if n.isdigit() else n):
            entry = entries.get(num)
            usage = entry.last_good if entry is not None else None
            hr = oauth.account_headroom(usage if isinstance(usage, dict) else None)
            if hr is None:
                continue
            if best_hr is None or hr > best_hr:
                best, best_hr = num, hr
        return best

    def _next_available(self, active: str | None, nums: list[str]) -> str:
        entries = self.usage_entries_by_account(fetch=None)
        data = self._read_roster()
        order = [n for n in self._seq_nums(data) if n in set(nums)]
        if not order:
            return nums[0]
        start = 0
        if active in order:
            start = order.index(active) + 1
        for i in range(len(order)):
            cand = order[(start + i) % len(order)]
            entry = entries.get(cand)
            usage = entry.last_good if entry is not None else None
            hr = oauth.account_headroom(usage if isinstance(usage, dict) else None)
            if hr is None or hr > 0:
                return cand
        return self._next_after(active, nums)

    # -- snapshot / usage -----------------------------------------------------

    def usage_entries_by_account(
        self, fetch: set[str] | None = None, *, scheduled: bool = False
    ) -> dict[str, UsageEntry]:
        return self._collect_usage_entries(fetch=fetch, scheduled=scheduled)

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        data = self._read_roster()
        entries = self._collect_usage_entries(fetch=fetch)
        live_slot = self._live_slot()
        accounts: list[AccountSnapshot] = []
        for num in self._seq_nums(data):
            rec = self._record(data, num)
            text = self._slot_text(num)
            ident = parse_auth(text)
            kind = rec.get("kind") or (ident.kind if ident else "oauth")
            switchable = ident is not None
            entry = entries.get(num, UsageEntry())
            accounts.append(
                AccountSnapshot(
                    number=str(num),
                    email=rec.get("email", "") or (ident.email if ident else ""),
                    org_name=rec.get("planType", "") or (ident.plan_type if ident else ""),
                    org_uuid=rec.get("accountId", "") or (ident.account_id if ident else ""),
                    is_active=str(num) == live_slot,
                    kind=kind,
                    switchable=switchable,
                    usage=entry,
                    alias=rec.get("alias", "") or "",
                    disabled=bool(rec.get("disabled")),
                    provider="codex",
                )
            )
        return AccountsSnapshot(
            active_number=live_slot,
            accounts=tuple(accounts),
            taken_at=self.clock(),
        )

    def _collect_usage_entries(
        self, fetch: set[str] | None = None, *, scheduled: bool = False
    ) -> dict[str, UsageEntry]:
        data = self._read_roster()
        nums = self._seq_nums(data)
        identities: dict[str, tuple[str, str]] = {}
        sentinels: dict[str, str] = {}
        homes: dict[str, Path] = {}
        live_slot = self._live_slot()
        for num in nums:
            rec = self._record(data, num)
            email = rec.get("email", "") or ""
            account_id = rec.get("accountId", "") or ""
            identities[num] = (email, account_id)
            text = self._slot_text(num)
            ident = parse_auth(text)
            if ident is None:
                sentinels[num] = USAGE_NO_CREDENTIALS
                continue
            if ident.kind == "api_key" or rec.get("kind") == "api_key":
                sentinels[num] = USAGE_API_KEY
                continue
            homes[num] = self.home if num == live_slot else self._slot_dir(num)
        models = self._poll_inputs[1] if self._poll_inputs else ()
        store = self._usage_store
        entries = store.entries(identities, models)
        requested = [
            num
            for num in nums
            if num not in sentinels and (fetch is None or num in fetch)
        ]
        if fetch is None:
            claims = store.reserve(
                requested, identities, respect_plans=True, repair_overslept=True
            )
        else:
            claims = store.reserve(
                requested,
                identities,
                respect_plans=False,
                repair_overslept=scheduled,
            )
        records: dict[str, FetchRecord] = {}
        if claims:
            bin_path = self._codex_bin()
            now = self.clock()
            for num in claims:
                if bin_path is None:
                    records[num] = FetchRecord(error="codex-not-installed")
                    continue
                home = homes.get(num, self._slot_dir(num))
                live_before = self._live_text() if home == self.home else None
                fetched_ok = False
                try:
                    limits = self._read_limits(home, codex_bin=bin_path)
                    usage = rate_limits_to_usage(limits, now)
                    records[num] = FetchRecord(usage=usage, error=None)
                    fetched_ok = True
                except Exception as exc:
                    self._logger.debug("codex usage read failed for %s: %r", num, exc)
                    records[num] = FetchRecord(error="app-server")
                if home == self.home:
                    live_changed = live_before != self._live_text()
                    if (fetched_ok or live_changed) and not self._capture_live(num):
                        records[num] = FetchRecord(error="app-server")
            store.record(records, identities, claims, None)
            entries = store.entries(identities, models)
        return {
            num: with_sentinel(entries.get(num, UsageEntry()), sentinels.get(num))
            for num in nums
        }

    def list_accounts(
        self,
        show_token_status: bool = False,
        json_output: bool = False,
        fetch: set[str] | None = None,
    ) -> dict | None:
        snap = self.accounts_snapshot(fetch=fetch)
        if json_output:
            rows = []
            for acc in snap.accounts:
                rows.append(
                    account_row(
                        int(acc.number) if acc.number.isdigit() else acc.number,
                        acc.email,
                        acc.org_name,
                        acc.org_uuid,
                        acc.is_active,
                        acc.usage.decision_value(),
                        usage_fetched_at=acc.usage.fetched_at,
                        usage_age_s=acc.usage.age_s,
                        last_good_usage=acc.usage.last_good,
                        alias=acc.alias,
                        disabled=acc.disabled,
                    )
                )
            return {
                "schemaVersion": SCHEMA_VERSION,
                "provider": "codex",
                "activeAccountNumber": (
                    int(snap.active_number)
                    if snap.active_number and snap.active_number.isdigit()
                    else snap.active_number
                ),
                "accounts": rows,
            }
        if not snap.accounts:
            print(dimmed("No Codex accounts."))
            return None
        print(bolded("Accounts:"))
        for i, acc in enumerate(snap.accounts):
            tag = acc.display_tag
            label = f"{accent(acc.alias)} ({acc.email})" if acc.alias else acc.email
            markers = ""
            if acc.is_active:
                markers += f" {bolded('(active)')}"
            if acc.disabled:
                markers += f" {muted('(disabled)')}"
            print(f"  {acc.number}: {label} {muted(f'[{tag}]')}{markers}")
            for line in _usage_entry_lines(acc.usage):
                print(f"     {line}")
            if i < len(snap.accounts) - 1:
                print()
        return None
