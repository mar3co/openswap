"""Named Claude Desktop profile storage and standalone launch.

Only metadata and new directories are written here. Claude owns sign-in,
cookies, secrets and conversation data. Opening a profile never stops another
process, copies credentials, changes the default login, or rotates accounts.
The separate selection coordinator owns linked Code + Desktop actions.
"""

from __future__ import annotations

import json
import hashlib
import os
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import threading
import unicodedata
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from xml.parsers.expat import ExpatError

from openswap.exceptions import ClaudeSwitchError
from openswap.locking import FileLock

APP_PATH = Path("/Applications/Claude.app")
BUNDLE_ID = "com.anthropic.claudefordesktop"
TEAM_ID = "Q6L2SF6YDW"
PROFILE_NOTICE = (
    "Experimental Claude Desktop profiles keep their own sign-ins. "
    "Link a saved Code account to select Desktop and Code together. "
    "Sign in inside Claude on first use; that profile keeps its own sign-in. "
    "Check the account in Claude before starting work. Opening a profile leaves other windows running. "
    "Chrome-extension pairing is currently unavailable in these profiles. "
    "Code and Cowork support is experimental."
)


class ClaudeDesktopError(ClaudeSwitchError):
    """A profile operation could not be performed safely."""


def default_root() -> Path:
    # These are user documents, not disposable OpenSwap credential backups.
    # In particular, brew uninstall --zap must not delete ongoing Claude work.
    return Path.home() / "Library" / "Application Support" / "OpenSwap Claude Desktop"


def _name(value: str) -> str:
    if not isinstance(value, str):
        raise ClaudeDesktopError("A profile needs a name.")
    value = unicodedata.normalize("NFC", value.strip())
    if not value or len(value) > 64 or any(unicodedata.category(c).startswith("C") for c in value):
        raise ClaudeDesktopError("Use a profile name of 1–64 characters without control characters.")
    return value


def _plain(path: Path, *, directory: bool = False) -> bool:
    """Never follow links in the managed namespace, including metadata/locks."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or (not directory and info.st_nlink != 1):
        raise ClaudeDesktopError("A Claude Desktop profile path is not a plain file or directory.")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ClaudeDesktopError("A Claude Desktop profile path belongs to another user.")
    if os.name != "nt" and info.st_mode & 0o077:
        raise ClaudeDesktopError("Claude Desktop profile storage must be private to your user (0700 folders / 0600 files).")
    return True


@dataclass(frozen=True)
class CodeAccount:
    store: str
    email: str
    organization: str

    @classmethod
    def for_identity(cls, root: Path, identity: tuple[str, str]):
        if not isinstance(identity, tuple) or len(identity) != 2:
            raise ClaudeDesktopError("Choose a saved Claude Code account first.")
        return cls.parse({"store": hashlib.sha256(str(root.resolve()).encode()).hexdigest(),
                          "email": identity[0], "organization": identity[1]})

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) != {"store", "email", "organization"}:
            raise ValueError("Invalid Code account link")
        if not isinstance(value["store"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["store"]):
            raise ValueError("Invalid Code account store")
        for key in ("email", "organization"):
            text = value[key]
            if not isinstance(text, str) or len(text) > 512 or any(unicodedata.category(c).startswith("C") for c in text):
                raise ValueError("Invalid Code account identity")
        if not value["email"]:
            raise ValueError("Missing Code account identity")
        return cls(**value)

    @property
    def identity(self):
        return self.email, self.organization


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    archived: bool = False
    code_account: CodeAccount | None = None


class DesktopProfiles:
    def __init__(self, root: Path | None = None):
        self.root = (root if root is not None else default_root()).absolute()
        self.registry = self.root / "profiles.json"

    def _root(self, *, create: bool = False) -> bool:
        if _plain(self.root, directory=True):
            return True
        if not create:
            return False
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        return _plain(self.root, directory=True)

    def _lock(self) -> FileLock:
        self._root(create=True)
        path = self.root / "profiles.lock"
        if not _plain(path):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except FileExistsError:
                _plain(path)
        return FileLock(path, timeout=0.5)

    def list(self, *, include_archived: bool = False) -> list[Profile]:
        if not self._root() or not _plain(self.registry):
            return []
        try:
            if self.registry.stat().st_size > 256_000:
                raise ValueError
            data = json.loads(self.registry.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or set(data) != {"version", "profiles"} or type(data["version"]) is not int or data["version"] not in (1, 2):
                raise ValueError
            rows = data["profiles"]
            if not isinstance(rows, list) or len(rows) > 100:
                raise ValueError
            profiles = []
            ids, names, links = set(), set(), set()
            for row in rows:
                keys = {"id", "name", "archived"} | ({"code_account"} if data["version"] == 2 else set())
                if not isinstance(row, dict) or set(row) != keys:
                    raise ValueError
                pid = row["id"]
                name = _name(row["name"])
                if not isinstance(pid, str) or not re.fullmatch(r"[0-9a-f]{32}", pid) or type(row["archived"]) is not bool:
                    raise ValueError
                if pid in ids or name.casefold() in names or name != row["name"]:
                    raise ValueError
                ids.add(pid)
                names.add(name.casefold())
                link = CodeAccount.parse(row["code_account"]) if row.get("code_account") is not None else None
                if link is not None:
                    if link in links:
                        raise ValueError("Duplicate Code account link")
                    links.add(link)
                profiles.append(Profile(pid, name, row["archived"], link))
            return [p for p in profiles if include_archived or not p.archived]
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise ClaudeDesktopError("Claude Desktop profile metadata is invalid. Restore profiles.json from a backup; it has not been replaced.") from exc

    def get(self, identifier: str, *, include_archived: bool = False) -> Profile:
        rows = self.list(include_archived=include_archived)
        # Exact stable IDs win, and names are unique even across archived rows.
        found = next((p for p in rows if p.id == identifier), None)
        if found is None:
            found = next((p for p in rows if p.name.casefold() == identifier.casefold()), None)
        if found is None:
            raise ClaudeDesktopError("Claude Desktop profile not found. List profiles, or restore an archived profile first.")
        return found

    def paths(self, profile: Profile) -> tuple[Path, Path]:
        if not re.fullmatch(r"[0-9a-f]{32}", profile.id):
            raise ClaudeDesktopError("Invalid Claude Desktop profile ID.")
        parent = self.root / profile.id
        return parent / "desktop", parent / "code"

    def _directories(self, profile: Profile, *, create: bool = False) -> tuple[Path, Path]:
        desktop, code = self.paths(profile)
        for path in (desktop.parent, desktop, code):
            if not _plain(path, directory=True):
                if not create:
                    raise ClaudeDesktopError("Profile data is missing. Restore its original folder before opening it; OpenSwap will not create a replacement login.")
                path.mkdir(mode=0o700)
        return desktop, code

    def _save(self, profiles: list[Profile]) -> None:
        _plain(self.registry)
        fd, raw = tempfile.mkstemp(prefix=".profiles-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 2, "profiles": [asdict(p) for p in profiles]}, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(raw, self.registry)
        finally:
            Path(raw).unlink(missing_ok=True)

    def add(self, name: str) -> Profile:
        name = _name(name)
        with self._lock():
            rows = self.list(include_archived=True)
            if any(p.name.casefold() == name.casefold() for p in rows):
                raise ClaudeDesktopError("That profile name already exists, possibly in Archived profiles.")
            if len(rows) >= 100:
                raise ClaudeDesktopError("This store already has 100 Claude Desktop profiles.")
            profile = Profile(uuid.uuid4().hex, name)
            self._directories(profile, create=True)
            self._save([*rows, profile])
            return profile

    def update(self, identifier: str, *, name: str | None = None, archived: bool | None = None) -> Profile:
        if name is not None:
            name = _name(name)
        if archived is not None and type(archived) is not bool:
            raise ClaudeDesktopError("Invalid archive state.")
        with self._lock():
            rows = self.list(include_archived=True)
            current = self.get(identifier, include_archived=True)
            if name is not None and any(p.id != current.id and p.name.casefold() == name.casefold() for p in rows):
                raise ClaudeDesktopError("That profile name already exists.")
            updated = replace(current, name=current.name if name is None else name,
                              archived=current.archived if archived is None else archived)
            self._save([updated if p.id == current.id else p for p in rows])
            return updated

    def linked(self, account: CodeAccount, *, include_archived: bool = False) -> Profile | None:
        return next((p for p in self.list(include_archived=include_archived) if p.code_account == account), None)

    def link(self, identifier: str, account: CodeAccount | None, *, expected: CodeAccount | None = None) -> Profile:
        if account is not None:
            account = CodeAccount.parse(asdict(account))
        with self._lock():
            rows = self.list(include_archived=True)
            current = self.get(identifier, include_archived=True)
            if expected is not None and current.code_account != expected:
                raise ClaudeDesktopError("This profile's account link changed. Open its menu again.")
            if current.archived and account is not None:
                raise ClaudeDesktopError("Restore this profile before linking it to an account.")
            if account is not None and any(p.id != current.id and p.code_account == account for p in rows):
                raise ClaudeDesktopError("This Code account already has a Desktop profile. Unlink that profile first, including if it is archived.")
            updated = replace(current, code_account=account)
            self._save([updated if p.id == current.id else p for p in rows])
            return updated

    def assign(self, identifier: str, account: CodeAccount) -> Profile:
        """Move this account's link atomically; never take another account's profile."""
        account = CodeAccount.parse(asdict(account))
        with self._lock():
            rows = self.list(include_archived=True)
            current = self.get(identifier)
            if current.code_account not in (None, account):
                raise ClaudeDesktopError("This profile is linked to another Code account. Unlink it before assigning it here.")
            updated = replace(current, code_account=account)
            self._save([updated if p.id == current.id else replace(p, code_account=None)
                        if p.code_account == account else p for p in rows])
            return updated

    def open(self, identifier: str, *, app: ClaudeDesktopApp | None = None) -> dict:
        app = app or ClaudeDesktopApp()
        info = app.validate()  # no profile changes or launch before publisher validation
        with self._lock():
            profile = self.get(identifier)
            desktop, code = self._directories(profile)
            result = app.open(desktop, code)
            return {**result, "profile": asdict(profile), "app_version": info["version"],
                    "identity_verified": False, "experimental": True}


def launch_environment(code: Path, source: dict[str, str] | None = None) -> dict[str, str]:
    """Keep OS/proxy/trust settings, never inherit an API login or developer flags.

    Organization policy is still read by Claude from its normal managed locations.
    Explicit managed-policy paths survive; user settings/secrets are not copied.
    """
    source = dict(os.environ if source is None else source)
    allowed = {"HOME", "USER", "LOGNAME", "SHELL", "PATH", "TMPDIR", "LANG", "__CF_USER_TEXT_ENCODING",
               "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "NODE_USE_SYSTEM_CA",
               "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
               "CLAUDE_CODE_MANAGED_SETTINGS_PATH", "CLAUDE_CODE_REMOTE_SETTINGS_PATH"}
    env = {k: v for k, v in source.items() if k in allowed or k.startswith("LC_")}
    env["CLAUDE_CONFIG_DIR"] = str(code)
    env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = str(code)
    return env


class ClaudeDesktopApp:
    def __init__(self, path: Path = APP_PATH, *, run=None, popen=None, platform: str | None = None):
        self.path = path
        self.binary = path / "Contents" / "MacOS" / "Claude"
        self.run = run or subprocess.run
        self.popen = popen or subprocess.Popen
        self.platform = sys.platform if platform is None else platform
        self._validation_stamp = None

    def _stamp(self) -> tuple:
        paths = (self.path / "Contents" / "Info.plist", self.binary,
                 self.path / "Contents" / "Resources" / "app.asar",
                 self.path / "Contents" / "_CodeSignature" / "CodeResources")
        values = []
        for path in paths:
            value = path.lstat()
            if not stat.S_ISREG(value.st_mode):
                raise ClaudeDesktopError("Claude Desktop contains an unexpected application file. Reinstall the official app.")
            values.append((value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_mode))
        return tuple(values)

    def validate(self) -> dict:
        self._validation_stamp = None
        if self.platform != "darwin":
            raise ClaudeDesktopError("Claude Desktop profiles are available on macOS only.")
        try:
            if self.path.is_symlink() or not self.path.is_dir() or self.binary.is_symlink() or not self.binary.is_file():
                raise ClaudeDesktopError("Install the official Claude.app in Applications before opening a profile.")
            before = self._stamp()
            info = plistlib.loads((self.path / "Contents" / "Info.plist").read_bytes())
            if not isinstance(info, dict) or info.get("CFBundleIdentifier") != BUNDLE_ID or info.get("CFBundleExecutable") != "Claude":
                raise ClaudeDesktopError("The installed app is not the expected Claude Desktop application.")
            verified = self.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(self.path)],
                                capture_output=True, text=True, timeout=30)
            details = self.run(["/usr/bin/codesign", "-d", "--verbose=4", str(self.path)],
                               capture_output=True, text=True, timeout=10)
            values = dict(line.split("=", 1) for line in details.stderr.splitlines() if "=" in line)
            if verified.returncode or details.returncode or values.get("Identifier") != BUNDLE_ID or values.get("TeamIdentifier") != TEAM_ID:
                raise ClaudeDesktopError("Claude's publisher signature could not be verified. Reinstall Claude from Anthropic and try again.")
            version = info.get("CFBundleShortVersionString")
            if not isinstance(version, str) or not version:
                raise ClaudeDesktopError("Claude Desktop did not report an application version.")
            if before != self._stamp():
                raise ClaudeDesktopError("Claude changed during validation, possibly due to an update. Try again after the update finishes.")
            self._validation_stamp = before
            return {"version": version, "publisher_verified": True, "experimental": True,
                    "identity_verified": False, "notice": PROFILE_NOTICE}
        except (OSError, ValueError, ExpatError, plistlib.InvalidFileException, subprocess.SubprocessError) as exc:
            raise ClaudeDesktopError("Could not inspect Claude Desktop. Check that it is installed and try again.") from exc

    def _processes(self) -> str:
        try:
            result = self.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ClaudeDesktopError("Could not check running Claude profiles. No new window was opened.") from exc
        if result.returncode:
            raise ClaudeDesktopError("Could not check running Claude profiles. No new window was opened.")
        return result.stdout

    def profile_states(self, store: DesktopProfiles) -> dict[str, str]:
        """Read process state once, without opening profiles or inspecting sign-ins."""
        profiles = store.list(include_archived=True)
        if not profiles:
            return {}
        try:
            processes = self._processes()
        except ClaudeDesktopError:
            return {profile.id: "Could not confirm" for profile in profiles}
        states = {}
        for profile in profiles:
            desktop, _code = store.paths(profile)
            try:
                pid = self._running([str(self.binary), f"--user-data-dir={desktop}"], desktop,
                                    processes=processes)
                states[profile.id] = "Running" if pid is not None else "Not running"
            except ClaudeDesktopError:
                states[profile.id] = "Could not confirm"
        return states

    def _running(self, argv: list[str], desktop: Path, *, processes: str | None = None) -> int | None:
        if processes is None:
            processes = self._processes()
        expected = " ".join(argv)
        matches = []
        for line in processes.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or not parts[0].isdigit():
                continue
            pid, command = parts
            if command == expected:
                matches.append(int(pid))
            elif f"--user-data-dir={desktop}" in command and "--type=" not in command:
                raise ClaudeDesktopError("Another launch is using this profile folder. Quit that profile in Claude before reopening it here.")
        if len(matches) > 1:
            raise ClaudeDesktopError("Multiple processes are using this profile. Quit them in Claude before reopening it.")
        return matches[0] if matches else None

    def open(self, desktop: Path, code: Path) -> dict:
        argv = [str(self.binary), f"--user-data-dir={desktop}"]
        pid = self._running(argv, desktop)
        try:
            if self._validation_stamp is None or self._validation_stamp != self._stamp():
                raise ClaudeDesktopError("Claude changed since validation. Try opening the profile again after the update finishes.")
        except OSError as exc:
            raise ClaudeDesktopError("Claude changed since validation. Wait for its update to finish and try again.") from exc
        if pid is not None:
            return {"state": "already_running", "pid": pid}
        try:
            proc = self.popen(argv, env=launch_environment(code), cwd=str(desktop.parent),
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True)
            # An immediate exit can be a failed launch OR a singleton handoff.
            # Neither establishes that this profile opened successfully.
            try:
                proc.wait(timeout=0.75)
            except subprocess.TimeoutExpired:
                # Reap the child later without keeping OpenSwap alive or stopping Claude.
                threading.Thread(target=proc.wait, daemon=True, name="claude-desktop-reaper").start()
                return {"state": "launched", "pid": proc.pid}
            raise ClaudeDesktopError("Claude exited during launch. The profile was kept; open it again after resolving the app's startup problem.")
        except (OSError, subprocess.SubprocessError) as exc:
            raise ClaudeDesktopError("Could not open Claude Desktop. The profile was kept; you can retry without signing in again.") from exc


def main(argv: list[str]) -> int:
    """Separate dispatch: never construct Engine or migrate Claude credentials."""
    import argparse

    parser = argparse.ArgumentParser(prog="openswap claude-desktop", description=PROFILE_NOTICE)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("list", "check", "path", "add", "open", "rename", "archive", "restore"):
        command = sub.add_parser(action)
        command.add_argument("--json", action="store_true")
        if action == "list":
            command.add_argument("--all", action="store_true", help="include archived profiles")
        elif action == "add":
            command.add_argument("name")
        elif action in {"open", "rename", "archive", "restore"}:
            command.add_argument("profile", help="profile name or stable ID")
            if action == "rename":
                command.add_argument("name")
    args = parser.parse_args(argv)
    store = DesktopProfiles()
    try:
        if args.action == "check":
            data = ClaudeDesktopApp().validate()
        elif args.action == "path":
            data = {"path": str(store.root)}
        elif args.action == "list":
            data = {"profiles": [asdict(p) for p in store.list(include_archived=args.all)]}
        elif args.action == "add":
            data = {"profile": asdict(store.add(args.name)), "state": "created"}
        elif args.action == "open":
            data = store.open(args.profile)
        else:
            values = {"name": args.name} if args.action == "rename" else {"archived": args.action == "archive"}
            data = {"profile": asdict(store.update(args.profile, **values)), "state": args.action}
        if args.json:
            print(json.dumps({"ok": True, **data}, ensure_ascii=False))
        elif args.action == "list":
            for p in data["profiles"]:
                print(f"{p['id']}  {p['name']}" + (" (archived)" if p["archived"] else ""))
            if not data["profiles"]:
                print('No Claude Desktop profiles. Add one with: openswap claude-desktop add "Personal"')
        elif args.action == "path":
            print(data["path"])
        elif args.action == "check":
            print(f"Claude {data['version']}: publisher verified. {PROFILE_NOTICE}")
        elif args.action == "open":
            verb = "is already running" if data["state"] == "already_running" else "was launched"
            print(f"{data['profile']['name']} {verb} (PID {data['pid']}). Check the account in Claude; sign in there on first use.")
            print(PROFILE_NOTICE)
        else:
            print(f"{data['profile']['name']}: {data['state']}. Profile data is kept.")
            if args.action == "add":
                print(f"Open it with: openswap claude-desktop open {data['profile']['id']}")
        return 0
    except (ClaudeSwitchError, OSError) as exc:
        message = str(exc) if isinstance(exc, ClaudeSwitchError) else "Could not access Claude Desktop profile storage. Check permissions and retry."
        if args.json:
            print(json.dumps({"ok": False, "error": message}))
        else:
            print(message, file=sys.stderr)
        return 1
