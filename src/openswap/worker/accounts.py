"""Which local accounts Remote tasks may pin, read from roster metadata only.

Plan 017: for Codex an eligible account is a roster slot, identified by the
stable opaque reference ``stable_account_identity("codex", accountId)``.
Claude accounts sit behind a separate authentication gate, and roster-backed
Claude profiles are presumptively excluded, so they are listed only to say
they are not supported yet; nothing here can pin one.

Everything here reads ``sequence.json`` metadata (slot number, email, alias,
``accountId``) and nothing else: no auth files, tokens or Keychain items.
The reads create no files, so the worker can check its pin at launch.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from openswap.exceptions import ClaudeSwitchError
from openswap.worker.leases import LeaseStateError, stable_account_identity

_ACCOUNT_REF_RE = re.compile(r"^codex:[0-9a-f]{64}$")


class AccountPinError(ClaudeSwitchError):
    """A pin request was refused; ``code`` is a stable, path-free reason."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CodexAccountChoice:
    number: str
    email: str
    alias: str | None
    # None for a slot with no ChatGPT account ID (an API key): it has no
    # stable identity, so it cannot be pinned.
    account_ref: str | None
    disabled: bool = False

    @property
    def eligible(self) -> bool:
        return self.account_ref is not None

    def label(self) -> str:
        name = self.email or "(no email)"
        if self.alias:
            name = f"{name} ({self.alias})"
        return f"{self.number} · {name}"


@dataclass(frozen=True)
class ClaudeAccountEntry:
    number: str
    email: str
    alias: str | None

    def label(self) -> str:
        name = self.email or "(no email)"
        if self.alias:
            name = f"{name} ({self.alias})"
        return f"{self.number} · {name}"


@dataclass(frozen=True)
class AccountChoices:
    pinned_ref: str | None
    codex: tuple[CodexAccountChoice, ...]
    claude: tuple[ClaudeAccountEntry, ...]

    @property
    def pinned(self) -> CodexAccountChoice | None:
        if self.pinned_ref is None:
            return None
        return next((c for c in self.codex if c.account_ref == self.pinned_ref), None)

    @property
    def pinned_missing(self) -> bool:
        """A pin is saved but no roster slot carries that account any more."""
        return self.pinned_ref is not None and self.pinned is None

    def to_dict(self) -> dict:
        pinned = self.pinned
        return {
            "pinned_account_ref": self.pinned_ref,
            "pinned_slot": pinned.number if pinned else None,
            "pinned_missing": self.pinned_missing,
            "codex": [
                {
                    "number": c.number, "email": c.email, "alias": c.alias,
                    "account_ref": c.account_ref, "eligible": c.eligible,
                    "disabled": c.disabled,
                    "pinned": c.account_ref is not None and c.account_ref == self.pinned_ref,
                }
                for c in self.codex
            ],
            "claude": [
                {
                    "number": c.number, "email": c.email, "alias": c.alias,
                    "eligible": False, "reason": "claude_auth_gate",
                }
                for c in self.claude
            ],
        }


def _roster_accounts(path: Path) -> dict | None:
    """``accounts`` of a roster file; ``None`` when unreadable or malformed."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(raw, dict):
        return None
    accounts = raw.get("accounts", {})
    return accounts if isinstance(accounts, dict) else None


def _slot_order(number: str) -> tuple[int, str]:
    return (int(number), "") if number.isdigit() else (1 << 30, number)


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _codex_ref(account_id) -> str | None:
    if not isinstance(account_id, str) or not account_id:
        return None
    try:
        return stable_account_identity("codex", account_id)
    except LeaseStateError:
        return None


def codex_accounts(backup_root: Path) -> tuple[CodexAccountChoice, ...] | None:
    """Codex roster slots in slot order; ``None`` when the roster is unreadable."""
    accounts = _roster_accounts(Path(backup_root) / "codex" / "sequence.json")
    if accounts is None:
        return None
    out = []
    for number, record in accounts.items():
        if not isinstance(record, dict):
            continue
        number = str(number)
        out.append(CodexAccountChoice(
            number=number,
            email=_text(record.get("email")),
            alias=_text(record.get("alias")) or None,
            account_ref=_codex_ref(record.get("accountId")),
            disabled=record.get("disabled") is True,
        ))
    return tuple(sorted(out, key=lambda c: _slot_order(c.number)))


def claude_accounts(backup_root: Path) -> tuple[ClaudeAccountEntry, ...]:
    """Claude roster slots, for display only; never eligible here."""
    accounts = _roster_accounts(Path(backup_root) / "sequence.json") or {}
    out = [
        ClaudeAccountEntry(
            number=str(number),
            email=_text(record.get("email")),
            alias=_text(record.get("alias")) or None,
        )
        for number, record in accounts.items()
        if isinstance(record, dict)
    ]
    return tuple(sorted(out, key=lambda c: _slot_order(c.number)))


def account_choices(backup_root: Path, pinned_ref: str | None) -> AccountChoices:
    return AccountChoices(
        pinned_ref=pinned_ref,
        codex=codex_accounts(backup_root) or (),
        claude=claude_accounts(backup_root),
    )


def codex_account_in_roster(backup_root: Path, account_ref: str) -> bool:
    """Whether a Codex roster slot still carries this account (fails closed)."""
    accounts = codex_accounts(backup_root)
    if not accounts or not isinstance(account_ref, str):
        return False
    return any(choice.account_ref == account_ref for choice in accounts)


def _names_claude_account(backup_root: Path, selector: str) -> bool:
    """Whether a selector Codex could not resolve names a Claude roster slot.

    Used only to explain a refusal, so it matches the number, alias and email
    forms ``openswap switch`` accepts; it never selects anything.
    """
    return any(
        selector in {entry.number, entry.email, entry.alias}
        for entry in claude_accounts(backup_root)
    )


def resolve_codex_selector(backup_root: Path, selector: str) -> CodexAccountChoice:
    """Resolve NUM|EMAIL|ALIAS (or an opaque ``codex:`` reference) to an eligible slot.

    Slot, alias and email use the Codex engine's own ``resolve_account``, the
    resolution behind ``openswap codex switch``. Call it while holding the
    Codex mutation guard so the slot cannot be removed or moved in between.
    """
    from openswap.exceptions import AccountNotFoundError, ConfigError

    root = Path(backup_root)
    selector = selector.strip() if isinstance(selector, str) else ""
    if not selector:
        raise AccountPinError("account_not_found")
    accounts = codex_accounts(root)
    if accounts is None:
        raise AccountPinError("roster_unavailable")
    if _ACCOUNT_REF_RE.fullmatch(selector):
        match = next((c for c in accounts if c.account_ref == selector), None)
        if match is None:
            raise AccountPinError("account_not_found")
        return match
    if selector.lower().startswith("claude:"):
        raise AccountPinError("claude_not_supported")
    from openswap.codex.engine import CodexEngine

    try:
        number, _email, _account_id = CodexEngine(backup_dir=root).resolve_account(selector)
    except AccountNotFoundError:
        if _names_claude_account(root, selector):
            raise AccountPinError("claude_not_supported") from None
        raise AccountPinError("account_not_found") from None
    except ConfigError:
        raise AccountPinError("account_ambiguous") from None
    match = next((c for c in accounts if c.number == str(number)), None)
    if match is None:
        raise AccountPinError("account_not_found")
    if not match.eligible:
        raise AccountPinError("account_not_eligible")
    return match
