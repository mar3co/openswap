"""Named Claude Desktop profile storage and standalone launch.

Only metadata and new directories are written here. Claude owns sign-in,
cookies, secrets and conversation data. Opening a profile never stops another
process, copies credentials, changes the default login, or rotates accounts.
"""

from __future__ import annotations

import json
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
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from pathlib import Path
from xml.parsers.expat import ExpatError

from openswap.exceptions import ClaudeSwitchError
from openswap.locking import FileLock

APP_PATH = Path("/Applications/Claude.app")
BUNDLE_ID = "com.anthropic.claudefordesktop"
TEAM_ID = "Q6L2SF6YDW"
# codesign evaluates this against the signature itself, so a lookalike that
# copies the identifier text cannot pass without Anthropic's certificate.
REQUIREMENT = f'anchor apple generic and identifier "{BUNDLE_ID}" and certificate leaf[subject.OU] = "{TEAM_ID}"'
REGISTRY_VERSION = 6
# A second open of the same profile waits this long for the first to finish.
LAUNCH_LOCK_TIMEOUT = 3
PROFILE_NOTICE = (
    "Claude Desktop profiles open side by side and keep separate sign-ins. "
    "Sign in and check the account inside each profile. "
    "Your terminal and editor Claude Code login stays unchanged. "
    "Check the account inside Code and Cowork. Chrome pairing is unavailable."
)


class ClaudeDesktopError(ClaudeSwitchError):
    """A profile operation could not be performed safely."""


def default_root() -> Path:
    # These are user documents, not disposable OpenSwap credential backups.
    # In particular, brew uninstall --zap must not delete ongoing Claude work.
    return Path.home() / "Library" / "Application Support" / "OpenSwap Claude Desktop"


def default_desktop_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "Claude"


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


BADGE_COLORS = {"orange": (0.73, 0.29, 0.17), "blue": (0.12, 0.34, 0.72),
                "purple": (0.48, 0.24, 0.68), "green": (0.12, 0.48, 0.34)}


def validate_badge(badge: str, color: str) -> None:
    if not isinstance(badge, str) or (badge and not re.fullmatch(r"[1-9][0-9]{0,2}", badge)):
        raise ClaudeDesktopError("Use a number from 1 to 999, or leave it blank for the standard icon.")
    if not isinstance(color, str) or color not in BADGE_COLORS:
        raise ClaudeDesktopError("Choose one of the available badge colors.")


def account_label(value: str, *, email: bool = False) -> str:
    """Optional display metadata, never an account key or launch constraint."""
    limit = 320 if email else 128
    if not isinstance(value, str):
        raise ClaudeDesktopError("Enter text for the account details, or leave them blank.")
    value = unicodedata.normalize("NFC", value.strip())
    if len(value) > limit or any(unicodedata.category(c).startswith("C") for c in value):
        raise ClaudeDesktopError(f"Keep account details under {limit + 1} characters, without control characters.")
    if email and value and not re.fullmatch(r"[^\s@]+@[^\s@]+", value):
        raise ClaudeDesktopError("Enter an email address, or leave it blank.")
    return value


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    archived: bool = False
    badge: str = "1"
    color: str = "orange"
    email: str = ""
    organization: str = ""
    detected_at: str = ""
    kind: str = "managed"
    auto_detect: bool = True
    account_uuid: str = ""
    organization_uuid: str = ""
    code_setup_key: str = ""
    code_setup_state: str = ""
    detected_login: str = ""


class DesktopProfiles:
    def __init__(self, root: Path | None = None, *, default_desktop: Path | None = None):
        self.root = (root if root is not None else default_root()).absolute()
        self.registry = self.root / "profiles.json"
        self.default_desktop = (default_desktop if default_desktop is not None else default_desktop_path()).absolute()

    def _root(self, *, create: bool = False) -> bool:
        if _plain(self.root, directory=True):
            return True
        if not create:
            return False
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        return _plain(self.root, directory=True)

    def _lock(self, name: str = "profiles.lock", *, timeout: float = 0.5) -> FileLock:
        """The registry lock guards metadata only; launches take ``launch.lock``."""
        self._root(create=True)
        path = self.root / name
        if not _plain(path):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except FileExistsError:
                _plain(path)
        return FileLock(path, timeout=timeout)

    def keychain_paused(self) -> bool:
        """A durable stop flag, shared by all Desktop profiles in this store."""
        path = self.root / "keychain-paused"
        return self._root() and _plain(path)

    def pause_keychain(self, paused: bool) -> None:
        # Record before a permission request so quitting during the dialog also
        # requires an explicit retry. This contains no credentials or identity.
        if type(paused) is not bool:
            raise ClaudeDesktopError("Invalid Keychain access state.")
        with self._lock():
            path = self.root / "keychain-paused"
            present = _plain(path)
            if not paused:
                path.unlink(missing_ok=True)
            elif not present:
                # O_EXCL | O_CREAT already refuses an existing link; retain
                # O_NOFOLLOW where available without requiring it for metadata.
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.close(fd)

    def list(self, *, include_archived: bool = False) -> list[Profile]:
        if not self._root() or not _plain(self.registry):
            return []
        try:
            if self.registry.stat().st_size > 256_000:
                raise ValueError
            data = json.loads(self.registry.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or set(data) != {"version", "profiles"} or data["version"] != REGISTRY_VERSION:
                raise ValueError
            rows = data["profiles"]
            if not isinstance(rows, list) or len(rows) > 100:
                raise ValueError
            profiles = []
            ids, names = set(), set()
            for row in rows:
                if not isinstance(row, dict) or set(row) != {f.name for f in fields(Profile)}:
                    raise ValueError
                pid = row["id"]
                name = _name(row["name"])
                if not isinstance(pid, str) or not re.fullmatch(r"[0-9a-f]{32}", pid) or type(row["archived"]) is not bool:
                    raise ValueError
                if pid in ids or name.casefold() in names or name != row["name"]:
                    raise ValueError
                ids.add(pid)
                names.add(name.casefold())
                validate_badge(row["badge"], row["color"])
                email = account_label(row["email"], email=True)
                organization = account_label(row["organization"])
                detected_at = row["detected_at"]
                if email != row["email"] or organization != row["organization"]:
                    raise ValueError
                self._validate_detection_time(detected_at, email, organization)
                if row["kind"] not in ("managed", "default") or type(row["auto_detect"]) is not bool:
                    raise ValueError
                if row["kind"] == "default" and any(p.kind == "default" for p in profiles):
                    raise ValueError
                code_identity = {k: row[k] for k in ("account_uuid", "organization_uuid", "code_setup_key", "code_setup_state", "detected_login")}
                self._validate_code_identity(detected_at=detected_at, **code_identity)
                profiles.append(Profile(pid, name, row["archived"], row["badge"], row["color"], email, organization,
                                        detected_at, row["kind"], row["auto_detect"], **code_identity))
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

    def paths(self, profile: Profile) -> tuple[Path, Path | None]:
        if not re.fullmatch(r"[0-9a-f]{32}", profile.id):
            raise ClaudeDesktopError("Invalid Claude Desktop profile ID.")
        if profile.kind == "default":
            return self.default_desktop, None
        parent = self.root / profile.id
        return parent / "desktop", parent / "code"

    def _default_exists(self) -> bool:
        """Claude owns this directory and its permissions. Never create/chmod it."""
        try:
            info = self.default_desktop.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise ClaudeDesktopError("The existing Claude profile folder could not be read safely.")
        return True

    def ensure_default(self) -> Profile | None:
        """Discover the ordinary profile once; preserve edits and archive state."""
        existing = next((p for p in self.list(include_archived=True) if p.kind == "default"), None)
        if existing is not None:
            return existing
        if not self._default_exists():
            return None
        with self._lock():
            rows = self.list(include_archived=True)
            existing = next((p for p in rows if p.kind == "default"), None)
            if existing is not None:
                return existing
            if not self._default_exists():
                return None
            if len(rows) >= 100:
                raise ClaudeDesktopError("This store already has 100 Claude Desktop profiles.")
            names = {p.name.casefold() for p in rows}
            name = next(n for n in ["Default", *(f"Default ({i})" for i in range(2, 102))] if n.casefold() not in names)
            profile = Profile(uuid.uuid4().hex, name, badge="", kind="default")
            self._save([profile, *rows])
            return profile

    def _directories(self, profile: Profile, *, create: bool = False) -> tuple[Path, Path | None]:
        desktop, code = self.paths(profile)
        if profile.kind == "default":
            if not self._default_exists():
                raise ClaudeDesktopError("The existing Claude profile folder is missing. Open Claude Desktop to restore it.")
            return desktop, None
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
                json.dump({"version": REGISTRY_VERSION, "profiles": [asdict(p) for p in profiles]}, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(raw, self.registry)
        finally:
            Path(raw).unlink(missing_ok=True)
        # The rename is in place either way; the directory sync only hastens durability.
        try:
            dir_fd = os.open(self.root, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)

    def add(self, name: str) -> Profile:
        name = _name(name)
        with self._lock():
            rows = self.list(include_archived=True)
            if any(p.name.casefold() == name.casefold() for p in rows):
                raise ClaudeDesktopError("That profile name already exists, possibly in Archived profiles.")
            if len(rows) >= 100:
                raise ClaudeDesktopError("This store already has 100 Claude Desktop profiles.")
            used = {p.badge for p in rows}
            badge = next(str(n) for n in range(1, 1000) if str(n) not in used)
            profile = Profile(uuid.uuid4().hex, name, badge=badge)
            self._directories(profile, create=True)
            self._save([*rows, profile])
            return profile

    @staticmethod
    def _validate_code_identity(*, detected_at="", account_uuid="", organization_uuid="", code_setup_key="", code_setup_state="", detected_login=""):
        for value in (account_uuid, organization_uuid):
            if not isinstance(value, str) or (value and not re.fullmatch(r"[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}", value)):
                raise ValueError("Invalid detected identity")
        if bool(account_uuid) != bool(organization_uuid) or (account_uuid and not detected_at):
            raise ValueError("Incomplete detected identity")
        if not isinstance(detected_login, str) or (detected_login and not re.fullmatch(r"[a-f0-9]{64}", detected_login)):
            raise ValueError("Invalid detected login")
        if bool(account_uuid) != bool(detected_login):
            raise ValueError("Missing detected login")
        if not isinstance(code_setup_key, str) or (code_setup_key and not re.fullmatch(r"[a-f0-9]{64}", code_setup_key)):
            raise ValueError("Invalid Code setup key")
        if code_setup_state not in ("", "authorizing", "connected", "sign_in", "install", "mismatch", "failed"):
            raise ValueError("Invalid Code setup state")
        if bool(code_setup_key) != bool(code_setup_state):
            raise ValueError("Incomplete Code setup state")

    @staticmethod
    def _validate_detection_time(value, email, organization):
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError("Invalid detection time")
        if value and (not email or not organization or datetime.fromisoformat(value).tzinfo is None):
            raise ValueError("Invalid detection time")

    def update(self, identifier: str, *, name: str | None = None, archived: bool | None = None,
               badge: str | None = None, color: str | None = None, email: str | None = None,
               organization: str | None = None, detected_at: str | None = None,
               auto_detect: bool | None = None, expected: Profile | None = None,
               account_uuid: str | None = None, organization_uuid: str | None = None,
               code_setup_key: str | None = None, code_setup_state: str | None = None,
               detected_login: str | None = None) -> Profile:
        if name is not None:
            name = _name(name)
        if archived is not None and type(archived) is not bool:
            raise ClaudeDesktopError("Invalid archive state.")
        if auto_detect is not None and type(auto_detect) is not bool:
            raise ClaudeDesktopError("Invalid automatic detection setting.")
        with self._lock():
            rows = self.list(include_archived=True)
            current = self.get(identifier, include_archived=True)
            if expected is not None and (current != expected or current.archived or not current.auto_detect):
                raise ClaudeDesktopError("Profile changed during account detection.")
            if name is not None and any(p.id != current.id and p.name.casefold() == name.casefold() for p in rows):
                raise ClaudeDesktopError("That profile name already exists.")
            new_badge = current.badge if badge is None else badge
            new_color = current.color if color is None else color
            validate_badge(new_badge, new_color)
            new_email = current.email if email is None else account_label(email, email=True)
            new_org = current.organization if organization is None else account_label(organization)
            # Editing either field removes the detection claim. Renames, badge
            # changes and archiving preserve it. Empty strings clear details.
            new_time = current.detected_at if email is None and organization is None else ""
            if detected_at is not None:
                new_time = detected_at
            # Display labels alone can never authorize or link a Code account.
            preserve_identity = bool(new_time and new_email == current.email and new_org == current.organization)
            ids = {
                "account_uuid": account_uuid if account_uuid is not None else (current.account_uuid if preserve_identity else ""),
                "organization_uuid": organization_uuid if organization_uuid is not None else (current.organization_uuid if preserve_identity else ""),
                "detected_login": detected_login if detected_login is not None else (current.detected_login if preserve_identity else ""),
                "code_setup_key": current.code_setup_key if code_setup_key is None else code_setup_key,
                "code_setup_state": current.code_setup_state if code_setup_state is None else code_setup_state,
            }
            try:
                self._validate_detection_time(new_time, new_email, new_org)
                self._validate_code_identity(detected_at=new_time, **ids)
            except ValueError as exc:
                raise ClaudeDesktopError("Invalid account detection details.") from exc
            updated = replace(current, name=current.name if name is None else name,
                              archived=current.archived if archived is None else archived,
                              badge=new_badge, color=new_color, email=new_email,
                              organization=new_org, detected_at=new_time,
                              auto_detect=current.auto_detect if auto_detect is None else auto_detect, **ids)
            self._save([updated if p.id == current.id else p for p in rows])
            return updated

    def open(self, identifier: str, *, app: ClaudeDesktopApp | None = None, existing_only=False) -> dict:
        app = app or ClaudeDesktopApp()
        info = app.validate()  # no profile changes or launch before publisher validation
        with self._lock():
            profile = self.get(identifier)
            desktop, code = self._directories(profile)
        launch = self._lock(f"launch-{profile.id}.lock", timeout=LAUNCH_LOCK_TIMEOUT)
        if launch.acquire():
            try:
                result = app.open(desktop, code, existing_only=True) if existing_only else app.open(desktop, code)
            finally:
                launch.release()
        else:
            # Another open of this profile is still launching: not an error.
            # It shows up as running once that launch is through.
            pid = app._running(app.launch_argv(desktop, code), desktop)
            result = {"state": "already_running", "pid": pid} if pid is not None else {"state": "already_opening", "pid": None}
        return {**result, "profile": asdict(profile), "app_version": info["version"],
                "identity_verified": False, "experimental": True}


def launch_environment(code: Path | None, source: dict[str, str] | None = None) -> dict[str, str]:
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
    if code is not None:
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

    def launch_argv(self, desktop: Path, code: Path | None) -> list[str]:
        return [str(self.binary)] if code is None else [str(self.binary), f"--user-data-dir={desktop}"]

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
            verified = self.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", f"-R={REQUIREMENT}", str(self.path)],
                                capture_output=True, text=True, timeout=30)
            if verified.returncode:
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
                argv = [str(self.binary)] if profile.kind == "default" else [str(self.binary), f"--user-data-dir={desktop}"]
                pid = self._running(argv, desktop,
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
            elif len(argv) == 1 and command.startswith(expected + " ") and "--user-data-dir" not in command and "--type=" not in command:
                # The ordinary app can receive a launch-services document/URL.
                matches.append(int(pid))
            elif re.search(r"--user-data-dir(?:=| )" + re.escape(str(desktop)) + r"(?:$|\s--)", command) and "--type=" not in command:
                raise ClaudeDesktopError("Another launch is using this profile folder. Quit that profile in Claude before reopening it here.")
        if len(matches) > 1:
            raise ClaudeDesktopError("Multiple processes are using this profile. Quit them in Claude before reopening it.")
        return matches[0] if matches else None

    def open(self, desktop: Path, code: Path | None, *, existing_only=False) -> dict:
        argv = self.launch_argv(desktop, code)
        pid = self._running(argv, desktop)
        try:
            if self._validation_stamp is None or self._validation_stamp != self._stamp():
                raise ClaudeDesktopError("Claude changed since validation. Try opening the profile again after the update finishes.")
        except OSError as exc:
            raise ClaudeDesktopError("Claude changed since validation. Wait for its update to finish and try again.") from exc
        if pid is not None:
            return {"state": "already_running", "pid": pid}
        if existing_only:
            # Attaching a Dock indicator must never reopen a session that quit
            # between the UI's launch result and the helper's startup.
            return {"state": "not_running", "pid": None}
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
    for action in ("list", "check", "path", "add", "open", "rename", "archive", "restore", "shortcut"):
        command = sub.add_parser(action)
        command.add_argument("--json", action="store_true")
        if action == "list":
            command.add_argument("--all", action="store_true", help="include archived profiles")
        elif action == "add":
            command.add_argument("name")
        elif action in {"open", "rename", "archive", "restore", "shortcut"}:
            command.add_argument("profile", help="profile name or stable ID")
            if action == "rename":
                command.add_argument("name")
            if action == "open":
                command.add_argument("--profile-root", type=Path, help=argparse.SUPPRESS)
                command.add_argument("--focus", action="store_true", help="bring the profile window forward")
                command.add_argument("--existing", action="store_true", help=argparse.SUPPRESS)
                command.add_argument("--from-launcher", action="store_true", help=argparse.SUPPRESS)
            if action == "shortcut":
                command.add_argument("--badge", help="number 1–999; empty string for the standard icon")
                command.add_argument("--color", choices=tuple(BADGE_COLORS))
                command.add_argument("--directory", type=Path, help="destination folder (default: ~/Applications/OpenSwap Profiles)")
    args = parser.parse_args(argv)
    store = DesktopProfiles(getattr(args, "profile_root", None))
    try:
        if args.action == "check":
            data = ClaudeDesktopApp().validate()
        elif args.action == "path":
            data = {"path": str(store.root)}
        elif args.action == "list":
            data = {"profiles": [asdict(p) for p in store.list(include_archived=args.all)]}
        elif args.action == "add":
            data = {"profile": asdict(store.add(args.name)), "state": "created"}
        elif args.action == "shortcut":
            from openswap.claude_desktop_shortcut import create_shortcut
            data = create_shortcut(store, args.profile, badge=args.badge, color=args.color, directory=args.directory)
        elif args.action == "open":
            data = store.open(args.profile, existing_only=True) if args.existing else store.open(args.profile)
            if data["state"] == "already_opening" and args.from_launcher:
                # The applet waits on a PID; without one it can only ask for another click.
                raise ClaudeDesktopError("Claude is still starting. Click this profile again in a moment.")
            if data.get("pid") and not args.from_launcher:
                from openswap.claude_desktop_presence import attach_presence
                try:
                    attach_presence(store, data["profile"]["id"])
                except (ClaudeSwitchError, OSError):
                    data["dock_warning"] = "Could not show the profile's running indicator. Choose Set up Dock shortcuts to retry."
            if args.focus and data.get("pid"):
                try:
                    from openswap.claude_desktop_menu import focus_profile
                    focus_profile(data["pid"])
                except ImportError:
                    pass  # the CLI without AppKit: the window stays where it is
                except Exception:
                    data["focus_warning"] = "Could not bring the profile window forward. Click it in the Dock."
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
        elif args.action == "shortcut":
            print(f"Shortcut created: {data['path']}. Drag it from Finder to your Dock.")
        elif args.action == "open":
            if data["state"] == "not_running":
                print(f"{data['profile']['name']} is not running.")
            elif data["state"] == "already_opening":
                print(f"{data['profile']['name']} is already opening.")
            else:
                verb = "is already running" if data["state"] == "already_running" else "was launched"
                print(f"{data['profile']['name']} {verb} (PID {data['pid']}). Check the account in Claude; sign in there on first use.")
                print(PROFILE_NOTICE)
            for key in ("dock_warning", "focus_warning"):
                if data.get(key):
                    print(data[key])
        else:
            print(f"{data['profile']['name']}: {data['state']}. Profile data is kept.")
            if args.action == "add":
                print(f"Open it with: openswap claude-desktop open {data['profile']['id']}")
        return 0
    except (ClaudeSwitchError, OSError) as exc:
        message = str(exc) if isinstance(exc, ClaudeSwitchError) else "Could not access Claude Desktop profile storage. Check permissions and retry."
    except Exception:
        if not getattr(args, "from_launcher", False):
            raise
        message = "Could not open this Claude profile. Open it from the OpenSwap menu to see details."
    if args.json:
        print(json.dumps({"ok": False, "error": message}))
    # The launcher shows stderr when the command fails, never stdout.
    if not args.json or getattr(args, "from_launcher", False):
        print(message, file=sys.stderr)
    return 1
