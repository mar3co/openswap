"""Opt-in Claude Code status line: wrap the user's command, append the account name.

Paint is store-only (live ``~/.claude.json`` + roster). It does not construct
the engine, hit the network, or write the default Claude login.

``--codex`` is paint-only: live ``auth.json`` + ``codex/sequence.json``. Codex
TUI has no command hook, so this module never writes ``config.toml``.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from openswap.codex.auth import parse_auth
from openswap.exceptions import ConfigError
from openswap.fsutil import replace_with_retry
from openswap.settings import SETTINGS_SCHEMA_VERSION, atomic_write_json, settings_path

PAINT_COMMAND = "openswap statusline"
INNER_TIMEOUT_S = 2.0


def account_label(
    email: str,
    *,
    alias: str = "",
    org_name: str = "",
    managed: bool = True,
) -> str:
    """Same title rule as extra cards: alias, else org, else personal.

    Unmanaged live logins (not in the roster) use the email local-part so we
    do not pretend they are a managed ``personal`` slot.
    """
    alias = (alias or "").strip()
    if alias:
        return alias
    org = (org_name or "").strip()
    if org:
        return org
    if managed:
        return "personal"
    email = email or ""
    return email.split("@", 1)[0] if email else ""


def append_label(text: str, label: str) -> str:
    """Put ``label`` on the row that already shows percentages."""
    label = (label or "").strip()
    if not label:
        return text
    if not text or not text.strip():
        return label
    ended_nl = text.endswith("\n")
    body = text[:-1] if ended_nl else text
    lines = body.split("\n")
    idx = None
    for i in range(len(lines) - 1, -1, -1):
        if "%" in lines[i]:
            idx = i
            break
    if idx is None:
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip():
                idx = i
                break
    if idx is None:
        return label + ("\n" if ended_nl else "")
    current = lines[idx].rstrip()
    if current.endswith(f" · {label}"):
        result = "\n".join(lines)
        return result + ("\n" if ended_nl else "")
    lines[idx] = f"{current} · {label}"
    result = "\n".join(lines)
    return result + ("\n" if ended_nl else "")


def _read_json(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _read_json_for_write(path: Path) -> dict:
    """Absent → {}. Existing but unreadable → ConfigError. Never overwrite unread."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(
            f"could not read {path}: {e}; refusing to overwrite it unread"
        ) from e
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"{path} is not valid JSON ({e}); refusing to overwrite it unread"
        ) from e
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path} is not a JSON object; refusing to overwrite it unread"
        )
    return raw


def live_identity(config_path: Path) -> tuple[str, str] | None:
    oauth = _read_json(config_path).get("oauthAccount") or {}
    if not isinstance(oauth, dict):
        return None
    email = oauth.get("emailAddress") or ""
    if not email:
        return None
    return email, oauth.get("organizationUuid") or ""


def current_account_label(config_path: Path, sequence_path: Path) -> str:
    ident = live_identity(config_path)
    if ident is None:
        return ""
    email, org = ident
    accounts = _read_json(sequence_path).get("accounts") or {}
    if not isinstance(accounts, dict):
        return account_label(email, managed=False)
    for rec in accounts.values():
        if not isinstance(rec, dict):
            continue
        if (rec.get("email") or "") != email:
            continue
        if (rec.get("organizationUuid") or "") != org:
            continue
        return account_label(
            email,
            alias=rec.get("alias") or "",
            org_name=rec.get("organizationName") or "",
            managed=True,
        )
    return account_label(email, managed=False)


def current_codex_account_label(auth_file: Path, sequence_path: Path) -> str:
    """Live Codex ``auth.json`` identity matched against ``codex/sequence.json``.

    Match is email + accountId. Label rule is ``account_label``: alias, else
    planType, else personal when managed; unmanaged uses the email local-part.
    """
    try:
        text = auth_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    ident = parse_auth(text)
    if ident is None:
        return ""
    email = ident.email or ""
    account_id = ident.account_id or ""
    accounts = _read_json(sequence_path).get("accounts") or {}
    if not isinstance(accounts, dict):
        return account_label(email, managed=False)
    for rec in accounts.values():
        if not isinstance(rec, dict):
            continue
        if (rec.get("email") or "") != email:
            continue
        if (rec.get("accountId") or "") != account_id:
            continue
        return account_label(
            email,
            alias=rec.get("alias") or "",
            org_name=rec.get("planType") or "",
            managed=True,
        )
    return account_label(email, managed=False)


def _cwd_from_stdin(stdin: str) -> str | None:
    try:
        data = json.loads(stdin) if stdin else {}
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    ws = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
    path = (ws or {}).get("current_dir") or data.get("cwd")
    if isinstance(path, str) and path:
        return path
    return None


def run_inner(command: str, stdin: str, cwd: str | None = None) -> str:
    try:
        proc = subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            text=True,
            shell=True,
            timeout=INNER_TIMEOUT_S,
            cwd=cwd or None,
            env=os.environ.copy(),
        )
        return proc.stdout or ""
    except Exception:
        return ""


def paint(
    stdin: str,
    *,
    inner_command: str | None,
    config_path: Path,
    sequence_path: Path,
) -> str:
    try:
        label = current_account_label(config_path, sequence_path)
    except Exception:
        label = ""
    inner_out = ""
    if inner_command:
        inner_out = run_inner(inner_command, stdin, cwd=_cwd_from_stdin(stdin))
    out = append_label(inner_out, label)
    if out and not out.endswith("\n"):
        out += "\n"
    return out


def paint_codex(*, auth_file: Path, sequence_path: Path) -> str:
    """Print the live Codex account label. No inner command, no Engine."""
    try:
        label = current_codex_account_label(auth_file, sequence_path)
    except Exception:
        label = ""
    if label and not label.endswith("\n"):
        label += "\n"
    return label


def load_wrap(backup_root: Path) -> dict:
    section = _read_json(settings_path(backup_root)).get("statusline")
    if not isinstance(section, dict):
        return {"innerCommand": None, "created": False}
    inner = section.get("innerCommand")
    if not (isinstance(inner, str) and inner.strip()):
        inner = None
    return {"innerCommand": inner, "created": bool(section.get("created"))}


def save_wrap(
    backup_root: Path,
    *,
    inner_command: str | None,
    created: bool,
) -> None:
    path = settings_path(backup_root)
    raw = _read_json_for_write(path)
    if inner_command is None and not created:
        raw.pop("statusline", None)
    else:
        raw["statusline"] = {
            "innerCommand": inner_command,
            "created": created,
        }
    raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
    backup_root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, raw)


def is_our_command(command: str | None) -> bool:
    if not command or not str(command).strip():
        return False
    s = str(command).strip()
    if s in {PAINT_COMMAND, "cswap statusline"}:
        return True
    try:
        parts = shlex.split(s)
    except ValueError:
        return False
    if len(parts) >= 2 and parts[-1] == "statusline":
        return Path(parts[-2]).name in {"openswap", "cswap"}
    return False


def paint_command() -> str:
    argv0 = Path(sys.argv[0])
    if argv0.name in {"openswap", "cswap"}:
        try:
            return f"{shlex.quote(str(argv0.resolve()))} statusline"
        except OSError:
            pass
    found = shutil.which("openswap")
    if found:
        return f"{shlex.quote(found)} statusline"
    return PAINT_COMMAND


def _write_json(path: Path, data: dict) -> None:
    """Atomic JSON write that does not chmod Claude's directory.

    Writes THROUGH a symlink, never over it (same shape as
    ``atomic_write_json``). A rename swaps a directory entry, so replacing
    onto a chezmoi/stow/nix link would detach it.
    """
    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        os.write(fd, json.dumps(data, indent=2).encode("utf-8") + b"\n")
        os.close(fd)
        fd = -1
        replace_with_retry(tmp_path, str(target))
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def install(
    config_home: Path,
    backup_root: Path,
    *,
    command: str = PAINT_COMMAND,
) -> dict:
    settings_file = config_home / "settings.json"
    # Read both files before writing either: a torn OpenSwap settings.json
    # must not leave Claude already wrapped.
    settings = _read_json_for_write(settings_file)
    _read_json_for_write(settings_path(backup_root))
    block = settings.get("statusLine")
    current = None
    if isinstance(block, dict):
        raw_cmd = block.get("command")
        current = raw_cmd if isinstance(raw_cmd, str) else None
        if is_our_command(current):
            return {"already": True, "created": False}
    created = not bool(current and current.strip())
    inner = None if created else current
    if not isinstance(block, dict):
        block = {}
    block["type"] = "command"
    block["command"] = command
    settings["statusLine"] = block
    # Wrap state first: if Claude write then fails, retry still has inner
    # and will not take the already-wrapped path that forgets it.
    save_wrap(backup_root, inner_command=inner, created=created)
    _write_json(settings_file, settings)
    return {"already": False, "created": created}


def uninstall(config_home: Path, backup_root: Path) -> dict:
    settings_file = config_home / "settings.json"
    settings = _read_json_for_write(settings_file)
    _read_json_for_write(settings_path(backup_root))
    wrap = load_wrap(backup_root)
    block = settings.get("statusLine")
    command = None
    if isinstance(block, dict):
        raw_cmd = block.get("command")
        command = raw_cmd if isinstance(raw_cmd, str) else None
    if not is_our_command(command):
        save_wrap(backup_root, inner_command=None, created=False)
        return {"restored": False}
    inner = wrap.get("innerCommand")
    if wrap.get("created") or not inner:
        settings.pop("statusLine", None)
    else:
        block["command"] = inner
        settings["statusLine"] = block
    _write_json(settings_file, settings)
    save_wrap(backup_root, inner_command=None, created=False)
    return {"restored": True}
