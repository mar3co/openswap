"""Shared policy persisted at ``<backup_root>/settings.json``.

This is CLI + extra *policy* (``autoswitch.*``, CLI ``ui.theme``), not extra
display (``menubar_settings.json``) and not engine cooldown state
(``autoswitch_state.json``). Written atomically with the backup dir's
0600/0700 modes. v1 carries the ``autoswitch`` and ``ui`` sections; other
sections can be added additively. Unknown keys survive a round trip.

Reading is forgiving: a missing or corrupt file yields defaults with a logged
warning, never a crash, so a bad hand edit degrades to default behavior.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from openswap.exceptions import ConfigError
from openswap.fsutil import replace_with_retry
from openswap.locking import FileLock

SETTINGS_SCHEMA_VERSION = 1
SETTINGS_FILENAME = "settings.json"

_logger = logging.getLogger("openswap")


@dataclass(frozen=True)
class AutoSwitchSettings:
    """Policy knobs for the auto-switch engine (``openswap auto``).

    ``threshold`` is binding-window utilization (max of the 5h/7d percentages):
    at or above it the engine looks for a better account. 90 rather than 95
    leaves margin for the macOS ~30s Keychain pickup tail and for heavy
    subagent turns burning past the mark before a swap lands. A proactive
    candidate must itself sit below the threshold (never land somewhere that
    re-triggers next tick) and beat the active account's utilization by at
    least ``hysteresis_pct``, so two accounts hovering at the line never
    ping-pong while a strictly better account is always taken.
    """

    threshold: float = 90.0
    interval_seconds: float = 60.0
    cooldown_seconds: float = 300.0
    hysteresis_pct: float = 10.0
    strategy: str = "best"  # "best" | "consume-first" (7-day reset) | "soonest-5h" (5-hour reset)
    include_api_key_accounts: bool = False
    unhealthy_ticks: int = 3
    # Comma-separated model display name(s) (e.g. "Fable" or "Fable,Opus"),
    # or "all" for every scoped window an account reports. Each named model's
    # per-model weekly limit is folded into the binding window, so the engine
    # switches off an account whose model quota is exhausted even while its
    # 5h/7d windows still have headroom. None = account-wide 5h/7d only
    # (default).
    model: str | None = None
    # Gate Codex AutoSwitchEngine construction and recheck before its auth commit.
    codex_enabled: bool = True


@dataclass(frozen=True)
class UiSettings:
    """Appearance preferences (``ui`` section). ``theme`` selects the CLI
    color theme; ``auto`` follows terminal-background detection."""

    theme: str = "auto"


@dataclass(frozen=True)
class WorkerWorkspace:
    """Locally approved mapping; never selected by remote task fields."""

    workspace_id: str
    output_root: Path
    readonly_roots: tuple[Path, ...] = ()


@dataclass(frozen=True)
class AllowlistedAccount:
    """One account the owner approved for a per-job choice by the control service.

    ``account_ref`` is random (``secrets.token_hex(16)``), generated once when
    the account is first allowlisted and never derived from the provider
    account ID, email or token: it is the only reference the service sees.
    ``identity`` is the local ``codex:`` identity (the same form as the pin)
    and never leaves this Mac. ``label`` is owner-chosen display text.
    """

    account_ref: str
    identity: str
    label: str


@dataclass(frozen=True)
class WorkerSettings:
    """Explicit local-worker policy. Both controls default to fail-closed."""

    enabled: bool = False
    paused: bool = False
    pinned_account_ref: str | None = None
    control_service_url: str | None = None
    # The worker ID the configured URL was paired as; a re-pair changes it.
    control_service_worker_id: str | None = None
    workspaces: tuple[WorkerWorkspace, ...] = ()
    # Accounts a control service may choose per job (the pin is the default
    # and is always one of them). Empty for settings written before the
    # allowlist existed: their pin migrates to a one-entry list on next write.
    account_allowlist: tuple[AllowlistedAccount, ...] = ()

    @property
    def default_account(self) -> AllowlistedAccount | None:
        return next(
            (entry for entry in self.account_allowlist if entry.identity == self.pinned_account_ref),
            None,
        )

    def allowlisted(self, account_ref: str) -> AllowlistedAccount | None:
        return next((entry for entry in self.account_allowlist if entry.account_ref == account_ref), None)


_SECTION_DEFAULT_SOURCES = {"autoswitch": AutoSwitchSettings, "ui": UiSettings}


@dataclass(frozen=True)
class SettingSpec:
    """Metadata for one user-tunable settings.json key.

    Single source of truth for bounds/choices: both the lenient clamp on load
    (`_clamped`) and the strict validation in `openswap config set`
    (`parse_setting_value`) read from here, so the two can't drift.
    """

    section: str  # top-level JSON section ("autoswitch", "ui")
    json_key: str  # camelCase key inside the section
    field: str  # snake_case AutoSwitchSettings field
    kind: str  # "float" | "int" | "bool" | "choice"
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()
    help: str = ""

    @property
    def dotted(self) -> str:
        return f"{self.section}.{self.json_key}"

    @property
    def default(self):
        return getattr(_SECTION_DEFAULT_SOURCES[self.section](), self.field)


# settings.json uses camelCase (matching the repo's other JSON artifacts);
# dataclass fields stay snake_case.
SETTING_SPECS: dict[str, SettingSpec] = {
    spec.dotted: spec
    for spec in (
        SettingSpec(
            "autoswitch", "threshold", "threshold", "float", 50.0, 99.9,
            help="Switch when the binding 5h/7d window reaches this pct",
        ),
        SettingSpec(
            "autoswitch", "intervalSeconds", "interval_seconds", "float", 15.0, 3600.0,
            help="Poll interval for the openswap auto loop, in seconds",
        ),
        SettingSpec(
            "autoswitch", "cooldownSeconds", "cooldown_seconds", "float", 0.0, 86400.0,
            help="Minimum seconds between proactive switches",
        ),
        SettingSpec(
            "autoswitch", "hysteresisPct", "hysteresis_pct", "float", 0.0, 50.0,
            help="A target must beat the active account by this many pct",
        ),
        SettingSpec(
            "autoswitch", "strategy", "strategy", "choice",
            choices=("best", "consume-first", "soonest-5h"),
            help=(
                "How auto-switch picks the target: best (most quota left), "
                "consume-first (burn weekly first: soonest 7-day reset), "
                "soonest-5h (burn 5-hour first: soonest 5-hour session reset)"
            ),
        ),
        SettingSpec(
            "autoswitch", "includeApiKeyAccounts", "include_api_key_accounts", "bool",
            help="Allow rotating onto managed API-key accounts (bill per token)",
        ),
        SettingSpec(
            "autoswitch", "unhealthyTicks", "unhealthy_ticks", "int", 1, 100,
            help="Consecutive failed polls before an account is unhealthy",
        ),
        SettingSpec(
            "autoswitch", "model", "model", "string",
            help="Also switch on these models' weekly limits (e.g. Fable, Fable,Opus, or all)",
        ),
        SettingSpec(
            "autoswitch", "codexEnabled", "codex_enabled", "bool",
            help="Rotate Codex CLI accounts alongside Claude",
        ),
        SettingSpec(
            "ui", "theme", "theme", "choice", choices=("dark", "light", "auto"),
            help="Color theme; auto follows the terminal background",
        ),
    )
}

_AUTOSWITCH_KEYS: dict[str, str] = {
    spec.field: spec.json_key
    for spec in SETTING_SPECS.values()
    if spec.section == "autoswitch"
}


def settings_path(backup_root: Path) -> Path:
    return backup_root / SETTINGS_FILENAME


def _settings_write_lock(backup_root: Path) -> FileLock:
    return FileLock(backup_root / ".settings.lock")


def parse_model_names(value: str | None) -> tuple[str, ...]:
    """Split a comma-separated model list, trimmed and case-insensitively
    deduped (first spelling wins). Shared by the auto engine and the manual
    switch strategies so both read ``autoswitch.model`` identically."""
    if not value:
        return ()
    seen: dict[str, str] = {}
    for part in value.split(","):
        name = part.strip()
        if name and name.lower() not in seen:
            seen[name.lower()] = name
    return tuple(seen.values())


def _clamped(settings: AutoSwitchSettings) -> AutoSwitchSettings:
    """Clamp values into the SETTING_SPECS ranges; bad types → the default."""

    def num(value, default: float, lo: float, hi: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return float(min(max(value, lo), hi))

    kwargs = {}
    for spec in SETTING_SPECS.values():
        if spec.section != "autoswitch":
            continue
        value = getattr(settings, spec.field)
        if spec.kind in ("float", "int"):
            clamped = num(value, spec.default, spec.lo, spec.hi)
            kwargs[spec.field] = int(clamped) if spec.kind == "int" else clamped
        elif spec.kind == "bool":
            kwargs[spec.field] = bool(value)
        elif spec.kind == "string":
            # A non-empty string keeps as-is; anything else reverts to default
            # (None) so a null/garbage settings.json value disables the filter.
            kwargs[spec.field] = value if isinstance(value, str) and value else spec.default
        else:  # choice
            if value not in spec.choices:
                _logger.warning(
                    "settings.json: unsupported %s %r; using %r",
                    spec.dotted, value, spec.default,
                )
                value = spec.default
            kwargs[spec.field] = value
    return AutoSwitchSettings(**kwargs)


def _read_raw(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        _logger.warning("Could not read %s (%s); using defaults", path, e)
        return {}
    if not isinstance(raw, dict):
        _logger.warning("%s is not a JSON object; using defaults", path)
        return {}
    return raw


def _autoswitch_from_raw(raw: dict) -> AutoSwitchSettings:
    """Parse the autoswitch section of an already-read settings.json object."""
    section = raw.get("autoswitch")
    if not isinstance(section, dict):
        return AutoSwitchSettings()
    kwargs = {}
    for field, json_key in _AUTOSWITCH_KEYS.items():
        if json_key in section:
            kwargs[field] = section[json_key]
    try:
        settings = AutoSwitchSettings(**kwargs)
    except TypeError:
        settings = AutoSwitchSettings()
    return _clamped(settings)


def _ui_from_raw(raw: dict) -> UiSettings:
    """Parse the ui section of an already-read settings.json object."""
    section = raw.get("ui")
    default = UiSettings()
    if not isinstance(section, dict):
        return default
    theme = section.get("theme", default.theme)
    if theme not in SETTING_SPECS["ui.theme"].choices:
        _logger.warning(
            "settings.json: unsupported ui.theme %r; using %r",
            theme, default.theme,
        )
        return default
    return UiSettings(theme=theme)


def load_settings(backup_root: Path) -> AutoSwitchSettings:
    """Load the autoswitch section; missing/corrupt file or fields → defaults."""
    return _autoswitch_from_raw(_read_raw(settings_path(backup_root)))


def load_ui_settings(backup_root: Path) -> UiSettings:
    """Load the ui section; missing/corrupt file or unknown theme → default."""
    return _ui_from_raw(_read_raw(settings_path(backup_root)))


_WORKSPACE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PINNED_ACCOUNT_RE = re.compile(r"^codex:[0-9a-f]{64}$")
_ALLOWLIST_REF_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_ACCOUNT_ALLOWLIST = 20
MAX_ACCOUNT_LABEL = 100


class AccountAllowlistFullError(ValueError):
    """Adding one more account would exceed MAX_ACCOUNT_ALLOWLIST."""


def valid_account_label(label: object) -> bool:
    """1-100 characters with no control characters (stricter than the wire's below-U+0020 rule)."""
    return (
        isinstance(label, str) and 1 <= len(label) <= MAX_ACCOUNT_LABEL
        and not any(unicodedata.category(c) == "Cc" for c in label)
    )


def new_allowlist_ref() -> str:
    """A fresh opaque reference: random, never derived from the account."""
    return secrets.token_hex(16)


def _allowlist_from_raw(value: object, pinned: str | None) -> tuple[AllowlistedAccount, ...]:
    """Parse ``accountAllowlist``; ``ValueError`` when anything is off."""
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_ACCOUNT_ALLOWLIST:
        raise ValueError
    entries: list[AllowlistedAccount] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"accountRef", "identity", "label"}:
            raise ValueError
        ref, identity, label = item["accountRef"], item["identity"], item["label"]
        if (not isinstance(ref, str) or not _ALLOWLIST_REF_RE.fullmatch(ref)
                or not isinstance(identity, str) or not _PINNED_ACCOUNT_RE.fullmatch(identity)
                or not valid_account_label(label)):
            raise ValueError
        entries.append(AllowlistedAccount(ref, identity, label))
    if (len({entry.account_ref for entry in entries}) != len(entries)
            or len({entry.identity for entry in entries}) != len(entries)):
        raise ValueError
    # The pinned default is always allowlisted, except in settings written
    # before the allowlist existed (no list at all), which migrate on write.
    if pinned is not None and entries and pinned not in {entry.identity for entry in entries}:
        raise ValueError
    return tuple(entries)


def _encode_allowlist(entries) -> list[dict[str, str]]:
    return [
        {"accountRef": entry.account_ref, "identity": entry.identity, "label": entry.label}
        for entry in entries
    ]


def _default_label(backup_root: Path, identity: str) -> str:
    try:
        from openswap.worker.accounts import default_account_label
        return default_account_label(backup_root, identity)
    except Exception:
        return "Codex account"


def _migrate_account_allowlist(section: dict, backup_root: Path) -> None:
    """Give a pin written before the allowlist existed its one-entry allowlist.

    Runs inside every worker-section write. Only the legacy shape (a valid pin
    and no ``accountAllowlist`` key) changes; the entry gets a fresh random
    reference and the default label (slot alias or "Codex account N").
    """
    pinned = section.get("pinnedAccountRef")
    if (section.get("accountAllowlist") is not None or not isinstance(pinned, str)
            or not _PINNED_ACCOUNT_RE.fullmatch(pinned)):
        return
    section["accountAllowlist"] = _encode_allowlist(
        (AllowlistedAccount(new_allowlist_ref(), pinned, _default_label(backup_root, pinned)),)
    )


def _default_worker_workspace(backup_root: Path) -> WorkerWorkspace:
    return WorkerWorkspace(
        workspace_id="research",
        output_root=(Path(backup_root) / "worker" / "research").resolve(),
    )


def _worker_from_raw(raw: dict, backup_root: Path) -> WorkerSettings:
    section = raw.get("worker")
    if not isinstance(section, dict):
        return WorkerSettings(workspaces=(_default_worker_workspace(backup_root),))
    enabled = section.get("enabled", False)
    paused = section.get("paused", False)
    if type(enabled) is not bool or type(paused) is not bool:
        _logger.warning("settings.json worker policy is invalid; disabling the worker")
        return WorkerSettings(workspaces=(_default_worker_workspace(backup_root),))
    pinned = section.get("pinnedAccountRef")
    if pinned is not None and (not isinstance(pinned, str) or not _PINNED_ACCOUNT_RE.fullmatch(pinned)):
        _logger.warning("settings.json worker account reference is invalid; disabling the worker")
        return WorkerSettings(workspaces=(_default_worker_workspace(backup_root),))
    control_url = section.get("controlServiceUrl")
    if control_url is not None:
        from openswap.worker.protocol import ProtocolError, validate_url
        try:
            control_url = validate_url(control_url)
        except ProtocolError:
            _logger.warning("settings.json worker control service URL is invalid; disabling the worker")
            return WorkerSettings(workspaces=(_default_worker_workspace(backup_root),))
    control_worker = section.get("controlServiceWorkerId")
    if control_worker is not None and (
            control_url is None or not isinstance(control_worker, str) or not control_worker
            or len(control_worker) > 200 or any(ord(c) < 32 for c in control_worker)):
        _logger.warning("settings.json worker control service identity is invalid; disabling the worker")
        return WorkerSettings(workspaces=(_default_worker_workspace(backup_root),))
    raw_workspaces = section.get("workspaces")
    workspaces: list[WorkerWorkspace] = []
    if raw_workspaces is None:
        workspaces = [_default_worker_workspace(backup_root)]
    elif isinstance(raw_workspaces, dict) and len(raw_workspaces) <= 16:
        try:
            for workspace_id, config in raw_workspaces.items():
                if not isinstance(workspace_id, str) or not _WORKSPACE_ID_RE.fullmatch(workspace_id):
                    raise ValueError
                if not isinstance(config, dict):
                    raise ValueError
                root_text = config.get("outputRoot")
                readonly_text = config.get("readonlyRoots", [])
                if (not isinstance(root_text, str) or len(root_text) > 2048
                        or not Path(root_text).is_absolute()
                        or not isinstance(readonly_text, list) or len(readonly_text) > 16
                        or any(not isinstance(item, str) or len(item) > 2048
                               or not Path(item).is_absolute() for item in readonly_text)):
                    raise ValueError
                output = Path(root_text).resolve()
                readonly = tuple(Path(item).resolve() for item in readonly_text)
                if any(
                    output == root or output.is_relative_to(root) or root.is_relative_to(output)
                    for root in readonly
                ):
                    raise ValueError
                workspaces.append(WorkerWorkspace(workspace_id, output, readonly))
        except (TypeError, ValueError, OSError):
            _logger.warning("settings.json worker workspace registry is invalid; disabling the worker")
            return WorkerSettings(workspaces=(_default_worker_workspace(backup_root),))
    else:
        _logger.warning("settings.json worker workspace registry is invalid; disabling the worker")
        return WorkerSettings(workspaces=(_default_worker_workspace(backup_root),))
    if not workspaces:
        _logger.warning("settings.json worker workspace registry is empty; disabling the worker")
        return WorkerSettings(workspaces=(_default_worker_workspace(backup_root),))
    try:
        allowlist = _allowlist_from_raw(section.get("accountAllowlist"), pinned)
    except (TypeError, ValueError):
        _logger.warning("settings.json worker account allowlist is invalid; disabling the worker")
        return WorkerSettings(workspaces=(_default_worker_workspace(backup_root),))
    return WorkerSettings(
        enabled=enabled,
        paused=paused,
        pinned_account_ref=pinned,
        control_service_url=control_url,
        control_service_worker_id=control_worker,
        workspaces=tuple(workspaces),
        account_allowlist=allowlist,
    )


def load_worker_settings(backup_root: Path) -> WorkerSettings:
    """Read default-off worker policy without creating directories or files."""
    return _worker_from_raw(_read_raw(settings_path(backup_root)), Path(backup_root))


def update_worker_settings(
    backup_root: Path,
    *,
    enabled: bool | None = None,
    paused: bool | None = None,
) -> WorkerSettings:
    """Atomically update explicit worker policy while preserving other settings."""
    if enabled is not None and type(enabled) is not bool:
        raise ValueError("enabled must be a bool or None")
    if paused is not None and type(paused) is not bool:
        raise ValueError("paused must be a bool or None")
    path = settings_path(backup_root)
    with _settings_write_lock(backup_root):
        raw = _read_raw_for_write(path)
        raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
        section = raw.get("worker")
        if not isinstance(section, dict):
            section = {}
        current = _worker_from_raw(raw, Path(backup_root))
        if not isinstance(section.get("workspaces"), dict):
            section["workspaces"] = {
                workspace.workspace_id: {
                    "outputRoot": str(workspace.output_root),
                    "readonlyRoots": [str(root) for root in workspace.readonly_roots],
                }
                for workspace in current.workspaces
            }
        section.setdefault("pinnedAccountRef", current.pinned_account_ref)
        section["enabled"] = current.enabled if enabled is None else enabled
        section["paused"] = current.paused if paused is None else paused
        _migrate_account_allowlist(section, Path(backup_root))
        raw["worker"] = section
        atomic_write_json(path, raw)
        return _worker_from_raw(raw, Path(backup_root))


def _encode_worker_workspaces(workspaces: tuple[WorkerWorkspace, ...]) -> dict[str, dict[str, object]]:
    """Validate an approved workspace registry and return its settings encoding."""
    if not isinstance(workspaces, tuple) or not 1 <= len(workspaces) <= 16:
        raise ValueError("one to sixteen approved workspaces are required")
    ids: set[str] = set()
    encoded: dict[str, dict[str, object]] = {}
    for workspace in workspaces:
        if (not isinstance(workspace, WorkerWorkspace)
                or not isinstance(workspace.workspace_id, str)
                or not _WORKSPACE_ID_RE.fullmatch(workspace.workspace_id)
                or workspace.workspace_id in ids):
            raise ValueError("workspace identifiers must be unique bounded names")
        ids.add(workspace.workspace_id)
        output = Path(workspace.output_root)
        readonly = tuple(Path(path) for path in workspace.readonly_roots)
        if not output.is_absolute() or len(str(output)) > 2048:
            raise ValueError("workspace output root must be an absolute local path")
        if len(readonly) > 16 or any(not path.is_absolute() or len(str(path)) > 2048 for path in readonly):
            raise ValueError("read-only roots must be bounded absolute paths")
        resolved_output = output.resolve()
        resolved_readonly = tuple(path.resolve() for path in readonly)
        if any(
            resolved_output == path
            or resolved_output.is_relative_to(path)
            or path.is_relative_to(resolved_output)
            for path in resolved_readonly
        ):
            raise ValueError("writable output and read-only roots must be disjoint")
        encoded[workspace.workspace_id] = {
            "outputRoot": str(resolved_output),
            "readonlyRoots": [str(path) for path in resolved_readonly],
        }
    return encoded


def _validate_pinned_account_ref(pinned_account_ref: str | None) -> None:
    if pinned_account_ref is not None and (
        not isinstance(pinned_account_ref, str)
        or not _PINNED_ACCOUNT_RE.fullmatch(pinned_account_ref)
    ):
        raise ValueError("pinned account reference is invalid")


def _decoded_workspaces(encoded: dict[str, dict[str, object]]) -> tuple[WorkerWorkspace, ...]:
    return tuple(
        WorkerWorkspace(workspace_id, Path(config["outputRoot"]),
                        tuple(Path(item) for item in config["readonlyRoots"]))
        for workspace_id, config in encoded.items()
    )


def _write_worker_local_policy(
    backup_root: Path, *, pinned_account_ref=None, encoded_workspaces=None,
    set_pin: bool, set_workspaces: bool, update_allowlist=None,
) -> WorkerSettings:
    """Read-modify-write the pin, registry and/or allowlist under the settings lock.

    A field not being set keeps its current value. ``update_allowlist`` maps
    the current ``(pin, allowlist)`` to the new pair, read under the same lock
    so concurrent edits cannot be lost. Setting a pin also allowlists it. The
    result is parsed back before it is written: a section that would fail
    closed (for example a malformed field this call does not touch) is
    refused, never written.
    """
    path = settings_path(backup_root)
    with _settings_write_lock(backup_root):
        raw = _read_raw_for_write(path)
        section = raw.get("worker")
        if not isinstance(section, dict):
            section = {}
        if set_pin:
            # Before the migration, so clearing a pre-allowlist pin does not
            # leave its account allowlisted, and a new pin is the one migrated.
            section["pinnedAccountRef"] = pinned_account_ref
        _migrate_account_allowlist(section, Path(backup_root))
        if set_pin and pinned_account_ref is not None:
            _allowlist_pin(section, Path(backup_root), pinned_account_ref)
        if update_allowlist is not None:
            current = _allowlist_from_raw(section.get("accountAllowlist"), None)
            pin, entries = update_allowlist(section.get("pinnedAccountRef"), current)
            _validate_pinned_account_ref(pin)
            entries = tuple(entries)
            if len(entries) > MAX_ACCOUNT_ALLOWLIST:
                raise AccountAllowlistFullError("account allowlist is full")
            if pin is not None and pin not in {entry.identity for entry in entries}:
                raise ValueError("the pinned default must stay allowlisted")
            section["pinnedAccountRef"] = pin
            section["accountAllowlist"] = _encode_allowlist(entries)
        if set_workspaces:
            section["workspaces"] = encoded_workspaces
        section.setdefault("enabled", False)
        section.setdefault("paused", False)
        raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
        raw["worker"] = section
        parsed = _worker_from_raw(raw, Path(backup_root))
        expected_allowlist = section.get("accountAllowlist")
        if (set_workspaces and parsed.workspaces != _decoded_workspaces(encoded_workspaces)) or (
            parsed.pinned_account_ref != section.get("pinnedAccountRef")
        ) or (
            expected_allowlist is not None
            and _encode_allowlist(parsed.account_allowlist) != expected_allowlist
        ):
            raise ValueError("worker local policy failed validation")
        atomic_write_json(path, raw)
        return parsed


def _allowlist_pin(section: dict, backup_root: Path, pinned_account_ref: str) -> None:
    """Add a newly pinned account to the allowlist when it is not there yet."""
    entries = _allowlist_from_raw(section.get("accountAllowlist"), None)
    if any(entry.identity == pinned_account_ref for entry in entries):
        return
    if len(entries) >= MAX_ACCOUNT_ALLOWLIST:
        raise AccountAllowlistFullError("account allowlist is full")
    section["accountAllowlist"] = _encode_allowlist((
        *entries,
        AllowlistedAccount(new_allowlist_ref(), pinned_account_ref,
                           _default_label(backup_root, pinned_account_ref)),
    ))


def configure_worker_local_policy(
    backup_root: Path,
    *,
    pinned_account_ref: str | None,
    workspaces: tuple[WorkerWorkspace, ...],
) -> WorkerSettings:
    """Persist locally approved opaque account/workspace references.

    This is a local configuration API only. Job submissions and IPC cannot
    override the pinned account or supply paths.
    """
    _validate_pinned_account_ref(pinned_account_ref)
    encoded = _encode_worker_workspaces(workspaces)
    return _write_worker_local_policy(
        backup_root, pinned_account_ref=pinned_account_ref, encoded_workspaces=encoded,
        set_pin=True, set_workspaces=True,
    )


def set_worker_pinned_account(backup_root: Path, pinned_account_ref: str | None) -> WorkerSettings:
    """Pin (or clear, with ``None``) the owner's local account; workspaces are kept.

    A newly pinned account is added to the account allowlist (default label,
    fresh random reference) when it is not already there; clearing the pin
    keeps the allowlist. Callers own eligibility: this checks only the opaque
    reference's shape.
    """
    _validate_pinned_account_ref(pinned_account_ref)
    return _write_worker_local_policy(
        backup_root, pinned_account_ref=pinned_account_ref, set_pin=True, set_workspaces=False,
    )


def update_worker_account_allowlist(backup_root: Path, update) -> WorkerSettings:
    """Atomically replace the pin and allowlist with ``update(pin, allowlist)``.

    ``update`` receives the current pin and allowlist (already migrated) under
    the settings lock and returns the new ``(pin, entries)``. The result must
    keep the pin allowlisted, at most MAX_ACCOUNT_ALLOWLIST entries, unique
    references and identities, and valid labels, or nothing is written.
    """
    return _write_worker_local_policy(
        backup_root, set_pin=False, set_workspaces=False, update_allowlist=update,
    )


def set_worker_workspaces(backup_root: Path, workspaces: tuple[WorkerWorkspace, ...]) -> WorkerSettings:
    """Replace the approved workspace registry; the pinned account is kept."""
    encoded = _encode_worker_workspaces(workspaces)
    return _write_worker_local_policy(
        backup_root, encoded_workspaces=encoded, set_pin=False, set_workspaces=True,
    )


def save_settings(backup_root: Path, settings: AutoSwitchSettings) -> None:
    """Write the autoswitch section, preserving unknown keys and sections."""
    path = settings_path(backup_root)
    with _settings_write_lock(backup_root):
        raw = _read_raw(path)
        raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
        section = raw.get("autoswitch")
        if not isinstance(section, dict):
            section = {}
        for field, json_key in _AUTOSWITCH_KEYS.items():
            section[json_key] = getattr(settings, field)
        raw["autoswitch"] = section
        atomic_write_json(path, raw)


def setting_spec(dotted_key: str) -> SettingSpec:
    """Look up a spec by dotted key; unknown keys raise with the valid list."""
    spec = SETTING_SPECS.get(dotted_key)
    if spec is None:
        raise ConfigError(
            f"unknown setting '{dotted_key}'\n"
            f"Valid keys: {', '.join(SETTING_SPECS)}"
        )
    return spec


_BOOL_WORDS = {
    "true": True, "1": True, "yes": True,
    "false": False, "0": False, "no": False,
}


def parse_setting_value(spec: SettingSpec, raw_value: str):
    """Strictly parse a CLI-provided string for `openswap config set`.

    Unlike the forgiving clamp on load, out-of-range or mistyped values raise
    ConfigError so the user learns about the problem when setting the value,
    not by silently degraded behavior at `openswap auto` time.
    """
    if spec.kind == "bool":
        # Never bool(str): bool("false") is True.
        parsed = _BOOL_WORDS.get(raw_value.strip().lower())
        if parsed is None:
            raise ConfigError(
                f"{spec.dotted} expects true or false (or 1/0, yes/no), "
                f"got '{raw_value}'"
            )
        return parsed
    if spec.kind == "choice":
        if raw_value not in spec.choices:
            raise ConfigError(
                f"{spec.dotted} must be one of: {', '.join(spec.choices)}"
            )
        return raw_value
    if spec.kind == "string":
        value = raw_value.strip()
        if not value:
            raise ConfigError(
                f"{spec.dotted} expects a non-empty value; use "
                f"'openswap config unset {spec.dotted}' to clear it"
            )
        return value
    try:
        value = int(raw_value) if spec.kind == "int" else float(raw_value)
    except ValueError:
        noun = "an integer" if spec.kind == "int" else "a number"
        raise ConfigError(
            f"{spec.dotted} expects {noun}, got '{raw_value}'"
        ) from None
    if not spec.lo <= value <= spec.hi:
        raise ConfigError(
            f"{spec.dotted} must be between {format_setting_value(spec.lo)} "
            f"and {format_setting_value(spec.hi)}"
        )
    return value


def format_setting_value(value) -> str:
    """Render a settings value the way settings.json writes it."""
    if value is None:
        return "(none)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _read_raw_for_write(path: Path) -> dict:
    """Raw read for the config write path: a corrupt file errors, never {}.

    ``_read_raw``'s degrade-to-defaults is right for reads, but a
    read-modify-write starting from ``{}`` would replace a malformed (and
    maybe hand-recoverable) file with a near-empty one.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"could not read {path}: {e}") from e
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"{path} is not valid JSON ({e}); fix or delete it before "
            "changing settings"
        ) from e
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path} is not a JSON object; fix or delete it before "
            "changing settings"
        )
    return raw


def set_setting(backup_root: Path, dotted_key: str, raw_value: str):
    """Validate and persist one key for `openswap config set`; returns the value.

    Writes only the given key (plus schemaVersion) — deliberately not
    ``save_settings``, which writes every known key and would freeze the
    current defaults into the file, pinning users to them if a later version
    changes a default. Unknown keys and sections in the file survive.
    """
    spec = setting_spec(dotted_key)
    value = parse_setting_value(spec, raw_value)
    path = settings_path(backup_root)
    with _settings_write_lock(backup_root):
        raw = _read_raw_for_write(path)
        raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
        section = raw.get(spec.section)
        if not isinstance(section, dict):
            section = {}
        section[spec.json_key] = value
        raw[spec.section] = section
        atomic_write_json(path, raw)
    return value


def unset_setting(backup_root: Path, dotted_key: str) -> bool:
    """Remove one key from settings.json; False if it wasn't set (no write)."""
    spec = setting_spec(dotted_key)
    path = settings_path(backup_root)
    with _settings_write_lock(backup_root):
        raw = _read_raw_for_write(path)
        section = raw.get(spec.section)
        if not isinstance(section, dict) or spec.json_key not in section:
            return False
        raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
        del section[spec.json_key]
        if not section:
            del raw[spec.section]
        atomic_write_json(path, raw)
        return True


def effective_settings(backup_root: Path) -> list[tuple[SettingSpec, object, bool]]:
    """(spec, effective value, explicitly set?) per key, in registry order.

    "Set" means the key is present in the raw file — an explicit value equal
    to the default still counts — so `openswap config`'s "(default)" marker
    reflects the file, not value equality.
    """
    raw = _read_raw(settings_path(backup_root))
    loaded = {
        "autoswitch": _autoswitch_from_raw(raw),
        "ui": _ui_from_raw(raw),
    }
    rows = []
    for spec in SETTING_SPECS.values():
        section = raw.get(spec.section)
        is_set = isinstance(section, dict) and spec.json_key in section
        rows.append((spec, getattr(loaded[spec.section], spec.field), is_set))
    return rows


def merged_with_cli(settings: AutoSwitchSettings, args) -> AutoSwitchSettings:
    """Overlay non-None CLI overrides (argparse Namespace) onto settings."""
    overrides = {}
    for attr, field in (
        ("threshold", "threshold"),
        ("interval", "interval_seconds"),
        ("cooldown", "cooldown_seconds"),
        ("include_api_key_accounts", "include_api_key_accounts"),
        ("model", "model"),
        ("strategy", "strategy"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            overrides[field] = value
    if not overrides:
        return settings
    return _clamped(dataclasses.replace(settings, **overrides))


def atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write JSON with the backup dir's 0600/0700 modes.

    Shared by settings.json and the autoswitch state file (and any future
    machine-local state files beside them).

    **Writes THROUGH a symlink, never over it.** A rename swaps a directory
    ENTRY and does not follow links, so renaming onto a symlinked path
    DETACHES the link: the write succeeds, the content is right, and the
    link target silently stops receiving updates — until something restores
    the link (a dotfiles deploy), taking every change written since with
    it. Same shape as #192/#193, which fixed ``session.py``'s own writer;
    this is the shared JSON writer. Three consequences, each deliberate:

    - A DANGLING link still writes where it points; linking a path is a
      request to write there.
    - The temp file is created beside the RESOLVED target, so the rename
      stays on one filesystem and remains atomic (beside the LINK it would
      hit EXDEV whenever the target lives on another mount).
    - The 0700 hardening stays on the directory openswap owns. Applying it to
      the resolved parent would narrow a directory belonging to something
      else, and raise ``PermissionError`` outright when that parent is not
      ours to chmod. The written file still gets 0600, and ``mkstemp``
      creates it 0600 to begin with, so the secret is never exposed.
    """
    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        # `path.parent`, NOT the target's: see the docstring.
        os.chmod(path.parent, 0o700)
    fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
        os.close(fd)
        fd = -1
        replace_with_retry(tmp_path, str(target))
        if sys.platform != "win32":
            os.chmod(str(target), 0o600)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def configure_worker_service(backup_root: Path, url: str | None, worker_id: str | None = None) -> WorkerSettings:
    """Persist the owner-selected URL (and the worker ID it was paired as); configuring
    it alone never enables remote work. Clearing the URL clears the worker ID too."""
    if url is not None:
        from openswap.worker.protocol import validate_url
        url = validate_url(url)
    with _settings_write_lock(backup_root):
        raw = _read_raw_for_write(settings_path(backup_root))
        raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
        section = raw.get("worker")
        if not isinstance(section, dict):
            # Like the other worker writers: a malformed section is replaced,
            # unrelated settings are kept, and configuration stays usable.
            section = {}
            raw["worker"] = section
        section["controlServiceUrl"] = url
        if url is not None and worker_id is not None:
            section["controlServiceWorkerId"] = worker_id
        else:
            section.pop("controlServiceWorkerId", None)
        _migrate_account_allowlist(section, Path(backup_root))
        atomic_write_json(settings_path(backup_root), raw)
    return load_worker_settings(backup_root)
