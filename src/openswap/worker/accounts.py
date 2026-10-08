"""Which local accounts Remote tasks may pin, read from roster metadata only.

Plan 017: an eligible account is a roster slot of either provider, pinned by
a typed opaque reference. A Codex slot is
``stable_account_identity("codex", accountId)``; a Claude slot (the owner's
decision of 2026-10-07: their own native login, on their own paired Macs, for
tasks they start themselves) is
``stable_account_identity("claude", email, organizationUuid)``, the identity
the Claude engine and kickoff already lease. The prefix decides which
provider runs the job.

Everything here reads ``sequence.json`` metadata (slot number, email, alias,
``accountId``/``organizationUuid``) and nothing else: no auth files, tokens or
Keychain items. The reads create no files, so the worker can check its pin at
launch.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from openswap.exceptions import ClaudeSwitchError
from openswap.settings import AllowlistedAccount
from openswap.worker.leases import LeaseStateError, stable_account_identity

_ACCOUNT_REF_RE = re.compile(r"^codex:[0-9a-f]{64}$")
_CLAUDE_REF_RE = re.compile(r"^claude:[0-9a-f]{64}$")
PROVIDERS = ("codex", "claude")


def provider_of(identity: str | None) -> str | None:
    """``"codex"`` or ``"claude"`` from a typed account identity, else ``None``."""
    if isinstance(identity, str):
        prefix = identity.split(":", 1)[0]
        if prefix in PROVIDERS and (_ACCOUNT_REF_RE.fullmatch(identity) or _CLAUDE_REF_RE.fullmatch(identity)):
            return prefix
    return None


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
    provider = "codex"

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
    organization_uuid: str = ""
    # None for a slot with no email (its identity cannot be checked).
    account_ref: str | None = None
    disabled: bool = False
    provider = "claude"

    @property
    def eligible(self) -> bool:
        return self.account_ref is not None

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
    # Accounts a control service may choose per job; the pin is the default.
    allowlist: tuple[AllowlistedAccount, ...] = ()

    def slot_for(self, identity: str):
        return next((c for c in (*self.codex, *self.claude) if c.account_ref == identity), None)

    @property
    def pinned(self):
        if self.pinned_ref is None:
            return None
        return self.slot_for(self.pinned_ref)

    @property
    def pinned_missing(self) -> bool:
        """A pin is saved but no roster slot carries that account any more."""
        return self.pinned_ref is not None and self.pinned is None

    def to_dict(self) -> dict:
        pinned = self.pinned
        allowed = {entry.identity for entry in self.allowlist}
        return {
            "pinned_account_ref": self.pinned_ref,
            "pinned_slot": pinned.number if pinned else None,
            "pinned_missing": self.pinned_missing,
            "pinned_provider": provider_of(self.pinned_ref),
            "codex": [
                {
                    "provider": "codex",
                    "number": c.number, "email": c.email, "alias": c.alias,
                    "account_ref": c.account_ref, "eligible": c.eligible,
                    "disabled": c.disabled,
                    "pinned": c.account_ref is not None and c.account_ref == self.pinned_ref,
                    "allowed": c.account_ref is not None and c.account_ref in allowed,
                }
                for c in self.codex
            ],
            # ``account_ref`` here is the random reference a control service
            # sees; ``identity`` is the local ``codex:`` identity it maps to.
            "allowlist": [
                {
                    "account_ref": entry.account_ref, "identity": entry.identity,
                    "label": entry.label, "default": entry.identity == self.pinned_ref,
                    "provider": provider_of(entry.identity),
                    "slot": slot.number if (slot := self.slot_for(entry.identity)) else None,
                    "in_roster": slot is not None,
                }
                for entry in self.allowlist
            ],
            "claude": [
                {
                    "provider": "claude",
                    "number": c.number, "email": c.email, "alias": c.alias,
                    "account_ref": c.account_ref, "eligible": c.eligible,
                    "disabled": c.disabled,
                    "pinned": c.account_ref is not None and c.account_ref == self.pinned_ref,
                    "allowed": c.account_ref is not None and c.account_ref in allowed,
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


def _claude_ref(email, organization) -> str | None:
    if not isinstance(email, str) or not email:
        return None
    organization = organization if isinstance(organization, str) else ""
    try:
        return stable_account_identity("claude", email, organization)
    except LeaseStateError:
        return None


def claude_accounts(backup_root: Path) -> tuple[ClaudeAccountEntry, ...] | None:
    """Claude roster slots in slot order; ``None`` when the roster is unreadable."""
    accounts = _roster_accounts(Path(backup_root) / "sequence.json")
    if accounts is None:
        return None
    out = [
        ClaudeAccountEntry(
            number=str(number),
            email=_text(record.get("email")),
            alias=_text(record.get("alias")) or None,
            organization_uuid=_text(record.get("organizationUuid")),
            account_ref=_claude_ref(record.get("email"), record.get("organizationUuid") or ""),
            disabled=record.get("disabled") is True,
        )
        for number, record in accounts.items()
        if isinstance(record, dict)
    ]
    return tuple(sorted(out, key=lambda c: _slot_order(c.number)))


def account_choices(
    backup_root: Path, pinned_ref: str | None, allowlist: tuple[AllowlistedAccount, ...] = (),
) -> AccountChoices:
    return AccountChoices(
        pinned_ref=pinned_ref,
        codex=codex_accounts(backup_root) or (),
        claude=claude_accounts(backup_root) or (),
        allowlist=tuple(allowlist),
    )


def default_account_label(backup_root: Path, identity: str) -> str:
    """The label an allowlisted account gets unless the owner names it.

    The slot alias, else "Codex account N" or "Claude account N"; never the email, which is sent to
    the control service only if the owner explicitly makes it the label.
    """
    provider = provider_of(identity)
    name = "Claude account" if provider == "claude" else "Codex account"
    accounts = (claude_accounts(Path(backup_root)) if provider == "claude" else codex_accounts(Path(backup_root))) or ()
    slot = next((c for c in accounts if c.account_ref == identity), None)
    if slot is None:
        return name
    alias = "".join(ch for ch in (slot.alias or "") if ch.isprintable()).strip()[:100]
    return alias or f"{name} {slot.number}"[:100]


def account_in_roster(backup_root: Path, account_ref: str) -> bool:
    """Whether the roster of the identity's provider still carries it (fails closed)."""
    provider = provider_of(account_ref)
    if provider == "codex":
        return codex_account_in_roster(backup_root, account_ref)
    if provider == "claude":
        accounts = claude_accounts(backup_root)
        return bool(accounts) and any(choice.account_ref == account_ref for choice in accounts)
    return False


def codex_account_in_roster(backup_root: Path, account_ref: str) -> bool:
    """Whether a Codex roster slot still carries this account (fails closed)."""
    accounts = codex_accounts(backup_root)
    if not accounts or not isinstance(account_ref, str):
        return False
    return any(choice.account_ref == account_ref for choice in accounts)


def resolve_claude_selector(backup_root: Path, selector: str) -> ClaudeAccountEntry:
    """Resolve a Claude roster slot by number, alias, email or opaque ``claude:`` reference."""
    selector = selector.strip() if isinstance(selector, str) else ""
    if not selector:
        raise AccountPinError("account_not_found")
    accounts = claude_accounts(Path(backup_root))
    if accounts is None:
        raise AccountPinError("roster_unavailable")
    if _CLAUDE_REF_RE.fullmatch(selector):
        match = next((c for c in accounts if c.account_ref == selector), None)
        if match is None:
            raise AccountPinError("account_not_found")
        return match
    matches = [c for c in accounts if c.number == selector] or [
        c for c in accounts if c.alias and c.alias == selector
    ] or [c for c in accounts if c.email and c.email.lower() == selector.lower()]
    if not matches:
        raise AccountPinError("account_not_found")
    if len(matches) > 1:
        raise AccountPinError("account_ambiguous")
    if not matches[0].eligible:
        raise AccountPinError("account_not_eligible")
    return matches[0]


def resolve_account_selector(backup_root: Path, selector: str):
    """Resolve a selector to an eligible Codex or Claude slot.

    ``codex:<slot|email|alias>`` and ``claude:<slot|email|alias>`` (or the
    opaque references) name the provider explicitly. A bare selector keeps its
    old meaning, a Codex slot, and falls back to the Claude roster only when
    no Codex slot matches it.
    """
    selector = selector.strip() if isinstance(selector, str) else ""
    if _CLAUDE_REF_RE.fullmatch(selector):
        return resolve_claude_selector(backup_root, selector)
    if _ACCOUNT_REF_RE.fullmatch(selector):
        return resolve_codex_selector(backup_root, selector)
    lowered = selector.lower()
    if lowered.startswith("claude:"):
        return resolve_claude_selector(backup_root, selector[len("claude:"):])
    if lowered.startswith("codex:"):
        return resolve_codex_selector(backup_root, selector[len("codex:"):])
    try:
        return resolve_codex_selector(backup_root, selector)
    except AccountPinError as error:
        if error.code not in {"account_not_found", "claude_not_supported"}:
            raise
        try:
            return resolve_claude_selector(backup_root, selector)
        except AccountPinError as claude_error:
            if claude_error.code == "account_not_found":
                raise error from None
            raise


def _names_claude_account(backup_root: Path, selector: str) -> bool:
    """Whether a selector Codex could not resolve names a Claude roster slot.

    Used only to explain a refusal, so it matches the number, alias and email
    forms ``openswap switch`` accepts; it never selects anything.
    """
    return any(
        selector in {entry.number, entry.email, entry.alias}
        for entry in claude_accounts(backup_root) or ()
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
    except (AttributeError, KeyError, TypeError, ValueError, OSError):
        # The engine reads raw roster records; a malformed one (say, a
        # non-object entry) is an unreadable roster, never a traceback.
        raise AccountPinError("roster_unavailable") from None
    match = next((c for c in accounts if c.number == str(number)), None)
    if match is None:
        raise AccountPinError("account_not_found")
    if not match.eligible:
        raise AccountPinError("account_not_eligible")
    return match
