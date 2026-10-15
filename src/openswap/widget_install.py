"""Install the macOS WidgetKit widget.

The widget is a signed Swift appex. Python cannot host WidgetKit, so it
needs an app around it, and there are two:

* The cask's ``OpenSwap.app`` already embeds the appex next to the frozen
  extra. ``openswap widget --install`` only registers it with ``pluginkit``,
  retires a widget host left over from a uv install, and reloads timelines
  through the bundled helper. Nothing is built and the bundle is never
  deleted.
* A uv install has no bundle, so ``openswap widget --install`` xcodebuilds the
  sources next to this package into a separate host app in
  ``~/Applications``. A LaunchAgent keeps that host alive so it can reload
  timelines when the menu bar extra writes a new snapshot.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from openswap import bundle
from openswap.exceptions import ClaudeSwitchError
from openswap.launch_agent import (
    _launchctl,
    _wait_until_unloaded,
    domain_target,
    is_loaded,
    log_paths,
    plist_path,
    service_target,
    status as launch_status,
)
from openswap.widget_snapshot import (
    WIDGET_APP_NAME,
    host_app_path,
    notify_widget_reload,
    widget_app_path,
)

LABEL = "com.opensoft.openswap.widget"
LEGACY_LABEL = "com.cswap.widget"
SCHEME = "OpenSwapWidget"
HOST_PRODUCT = "OpenSwap"
# The separately built Swift host. The cask's app is `com.opensoft.openswap`.
HOST_BUNDLE_ID = "com.opensoft.openswap.widget"


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise ClaudeSwitchError("The Desktop widget is only available on macOS.")


def project_dir() -> Path:
    """Directory that contains ``OpenSwapWidget.xcodeproj``.

    Editable checkouts keep sources at ``<repo>/macos/OpenSwapWidget``. A wheel
    may vendor them next to this module as ``macos_widget``.
    """
    here = Path(__file__).resolve().parent
    for base in (here, *here.parents):
        candidate = base / "macos" / "OpenSwapWidget"
        if (candidate / "OpenSwapWidget.xcodeproj").exists():
            return candidate
    vendored = here / "macos_widget"
    if (vendored / "OpenSwapWidget.xcodeproj").exists():
        return vendored
    raise ClaudeSwitchError(
        "Widget sources were not found. Install openswap from the git "
        "checkout (editable) so macos/OpenSwapWidget is on disk."
    )


def detect_development_team() -> str:
    """Apple team id for Automatic signing.

    Prefers ``DEVELOPMENT_TEAM``, then the team already on an installed
    widget app (so rebuilds do not flip identifiers), then Xcode's
    last-selected team when that team has a local Apple Development cert,
    then any local cert's OU.
    """
    env = os.environ.get("DEVELOPMENT_TEAM", "").strip()
    if env:
        return env
    installed = _team_from_app(widget_app_path())
    if installed:
        return installed
    local = _signing_team_ids()
    last = _xcode_last_team()
    if last and (not local or last in local):
        return last
    if local:
        return local[0]
    raise ClaudeSwitchError(
        "No Apple Development team found. Open Xcode, sign in with an Apple "
        "ID (Settings → Accounts), or set DEVELOPMENT_TEAM."
    )


def _team_from_codesign_output(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("TeamIdentifier="):
            tid = line.split("=", 1)[1].strip()
            if tid and tid != "not set":
                return tid
    return None


def _team_from_app(app: Path) -> str | None:
    if not app.exists():
        return None
    try:
        proc = subprocess.run(
            ["codesign", "-dv", str(app)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return _team_from_codesign_output(proc.stderr or proc.stdout or "")


def _xcode_last_team() -> str | None:
    path = Path.home() / "Library/Preferences/com.apple.dt.Xcode.plist"
    try:
        data = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return None
    team = data.get("IDEProvisioningTeamManagerLastSelectedTeamID")
    return str(team) if team else None


def _signing_team_ids() -> list[str]:
    try:
        pems = subprocess.run(
            ["security", "find-certificate", "-a", "-c", "Apple Development", "-p"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    teams: list[str] = []
    buf: list[str] = []
    for line in pems.stdout.splitlines():
        buf.append(line)
        if "-----END CERTIFICATE-----" not in line:
            continue
        parsed = subprocess.run(
            ["openssl", "x509", "-noout", "-subject"],
            input="\n".join(buf),
            capture_output=True,
            text=True,
            check=False,
        )
        buf = []
        subject = parsed.stdout.replace("subject=", "")
        for part in subject.split(","):
            part = part.strip()
            if part.startswith("OU=") and part[3:] not in teams:
                teams.append(part[3:])
    return teams


def _xcodebuild() -> str:
    path = shutil.which("xcodebuild")
    if not path:
        raise ClaudeSwitchError(
            "xcodebuild not found. Install Xcode from the App Store and run "
            "`xcode-select --install`."
        )
    return path


def _host_binary(app: Path) -> Path:
    return app / "Contents" / "MacOS" / HOST_PRODUCT


def build_host_plist(app: Path, home: Path | None = None) -> bytes:
    out_log, err_log = log_paths(LABEL, home)
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": [str(_host_binary(app))],
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ProcessType": "Interactive",
            "StandardOutPath": str(out_log),
            "StandardErrorPath": str(err_log),
        }
    )


def install_launch_agent(app: Path, home: Path | None = None, uid: int | None = None) -> dict:
    """Keep the widget host running so snapshot notifications reload timelines."""
    _require_macos()
    if not _host_binary(app).is_file():
        raise ClaudeSwitchError(f"Widget host binary missing in {app}")
    target = plist_path(LABEL, home)
    out_log, err_log = log_paths(LABEL, home)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        out_log.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(build_host_plist(app, home))
    except OSError as e:
        raise ClaudeSwitchError(f"Could not write the launch agent: {e}") from e

    if is_loaded(LEGACY_LABEL, uid):
        _launchctl("bootout", service_target(LEGACY_LABEL, uid))
        _wait_until_unloaded(LEGACY_LABEL, uid)
    try:
        plist_path(LEGACY_LABEL, home).unlink(missing_ok=True)
    except OSError as e:
        raise ClaudeSwitchError(f"Could not remove the old widget launch agent: {e}") from e

    settled = True
    if is_loaded(LABEL, uid):
        _launchctl("bootout", service_target(LABEL, uid))
        settled = _wait_until_unloaded(LABEL, uid)

    booted = _launchctl("bootstrap", domain_target(uid), str(target))
    if booted.returncode != 0:
        detail = (booted.stderr or booted.stdout or "").strip()
        if not settled:
            detail = f"{detail}; the previous instance was still shutting down".lstrip("; ")
        raise ClaudeSwitchError(
            f"launchctl bootstrap failed (exit {booted.returncode})"
            + (f": {detail}" if detail else "")
        )
    return {
        "label": LABEL,
        "plist": str(target),
        "stdout_log": str(out_log),
        "stderr_log": str(err_log),
    }


def uninstall_launch_agent(home: Path | None = None, uid: int | None = None) -> dict:
    _require_macos()
    from openswap.launch_agent import uninstall as _uninstall

    result = _uninstall(label=LABEL, home=home, uid=uid)
    _uninstall(label=LEGACY_LABEL, home=home, uid=uid)
    return result


def _copy_built_app(derived: Path, dest: Path) -> Path:
    built = derived / "Build" / "Products" / "Release" / f"{HOST_PRODUCT}.app"
    if not built.is_dir():
        raise ClaudeSwitchError(f"xcodebuild produced no app at {built}")
    try:
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(built, dest)
    except OSError as e:
        raise ClaudeSwitchError(f"Could not install the widget app: {e}") from e
    return dest


def _pluginkit(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["pluginkit", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:  # pragma: no cover - pluginkit is in the base OS
        raise ClaudeSwitchError("pluginkit not found; is this macOS?") from e


def _pluginkit_or_raise(action: str, *args: str) -> None:
    proc = _pluginkit(*args)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ClaudeSwitchError(
            f"pluginkit could not {action} the widget (exit {proc.returncode})"
            + (f": {detail}" if detail else "")
        )


def _bundle_id(app: Path) -> str | None:
    try:
        data = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    value = data.get("CFBundleIdentifier") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def legacy_host(home: Path | None = None) -> Path | None:
    """The Swift widget host a uv install built, when it is still installed.

    Identified by its bundle id, so the cask's own app is never mistaken for
    it even when the cask was installed into ``~/Applications``.
    """
    app = host_app_path(home)
    if not app.is_dir() or app.resolve() == bundle.app_path():
        return None
    return app if _bundle_id(app) == HOST_BUNDLE_ID else None


def _move_to_trash(path: Path) -> None:
    """Move ``path`` to the user's Trash, like Finder; a missing path is fine."""
    try:
        from Foundation import NSURL, NSFileManager

        ok, _url, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
            NSURL.fileURLWithPath_(str(path)), None, None
        )
    except Exception as e:
        raise ClaudeSwitchError(f"Could not move {path} to the Trash: {e}") from e
    if ok or not path.exists():
        return
    reason = err.localizedDescription() if err is not None else "unknown error"
    raise ClaudeSwitchError(f"Could not move {path} to the Trash: {reason}")


def retire_legacy_host(home: Path | None = None, uid: int | None = None) -> Path | None:
    """Replace the uv install's widget host with the appex in this bundle.

    Two apps carrying the same appex id give WidgetKit two providers, so the
    old host goes: its LaunchAgent is booted out and its plist removed, its
    appex unregistered, and the app moved to the Trash (recoverable, unlike
    ``rmtree``). Then the bundled appex is registered. Returns the old app's
    path, or None when there was nothing to retire.
    """
    _require_macos()
    old = legacy_host(home)
    if old is None:
        return None
    uninstall_launch_agent(home=home, uid=uid)
    _pluginkit("-r", str(old / "Contents" / "PlugIns" / bundle.APPEX_NAME))
    _move_to_trash(old)
    appex = bundle.appex()
    if appex is not None:
        _pluginkit_or_raise("register", "-a", str(appex))
    return old


def _install_bundled_widget(home: Path | None = None, uid: int | None = None) -> dict:
    app = bundle.app_path()
    appex = bundle.appex()
    if app is None or appex is None or not appex.is_dir():
        raise ClaudeSwitchError(f"The widget extension is missing from {app}.")
    _pluginkit_or_raise("register", "-a", str(appex))
    retire_legacy_host(home=home, uid=uid)
    notify_widget_reload()
    return {"app": str(app), "label": "", "plist": ""}


def install_widget(
    *,
    home: Path | None = None,
    derived: Path | None = None,
    team: str | None = None,
) -> dict:
    """Build, copy to ~/Applications, load the host LaunchAgent, and open it.

    The frozen app builds nothing: it registers the appex it already carries.
    """
    _require_macos()
    if bundle.is_frozen():
        return _install_bundled_widget(home=home)
    src = project_dir()
    team = team or detect_development_team()
    derived = derived or (Path.home() / "Library" / "Caches" / "openswap-widget")
    log_file = derived / "xcodebuild.log"
    try:
        derived.mkdir(parents=True, exist_ok=True)
        handle = log_file.open("w", encoding="utf-8")
    except OSError as e:
        raise ClaudeSwitchError(f"Could not write the build log: {e}") from e
    cmd = [
        _xcodebuild(),
        "-project",
        str(src / "OpenSwapWidget.xcodeproj"),
        "-scheme",
        SCHEME,
        "-configuration",
        "Release",
        "-derivedDataPath",
        str(derived),
        "-destination",
        "generic/platform=macOS",
        f"DEVELOPMENT_TEAM={team}",
        "CODE_SIGN_STYLE=Automatic",
        "-allowProvisioningUpdates",
        "build",
    ]
    with handle:
        proc = subprocess.run(cmd, stdout=handle, stderr=subprocess.STDOUT, check=False)
    if proc.returncode != 0:
        tail = ""
        try:
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = "\n".join(lines[-20:])
        except OSError:
            pass
        raise ClaudeSwitchError(
            f"Widget build failed (xcodebuild exit {proc.returncode}). "
            f"Log: {log_file}"
            + (f"\n{tail}" if tail else "")
        )

    dest = widget_app_path(home)
    _copy_built_app(derived, dest)
    agent = install_launch_agent(dest, home=home)
    try:
        subprocess.run(
            ["/usr/bin/open", "-ga", str(dest)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass
    return {
        "app": str(dest),
        "team": team,
        "log": str(log_file),
        **agent,
    }


def uninstall_widget(home: Path | None = None, uid: int | None = None) -> dict:
    """Remove the built widget host, or unregister the frozen app's appex.

    The frozen app is the cask's to remove; this never deletes it.
    """
    _require_macos()
    if bundle.is_frozen():
        appex = bundle.appex()
        if appex is None:
            raise ClaudeSwitchError("The widget extension is missing from this app.")
        _pluginkit_or_raise("unregister", "-r", str(appex))
        return {
            "app": str(bundle.app_path()),
            "unregistered": True,
            "removed_app": False,
            "removed_plist": False,
            "was_loaded": False,
        }
    agent = uninstall_launch_agent(home=home, uid=uid)
    app = widget_app_path(home)
    removed_app = app.exists()
    if removed_app:
        try:
            shutil.rmtree(app)
        except FileNotFoundError:
            pass
        except OSError as e:
            raise ClaudeSwitchError(f"Could not remove the widget app: {e}") from e
    return {**agent, "removed_app": removed_app, "app": str(app)}


def _appex_registrations() -> list[str]:
    """One entry per registered copy of the appex: its path when pluginkit
    prints one, else the line itself.

    ``-D`` lists every physical copy; without it pluginkit returns only the
    last instance of the highest version, which hides a duplicate provider.
    """
    proc = _pluginkit("-m", "-D", "-v", "-i", bundle.APPEX_ID)
    if proc.returncode != 0:
        return []
    found: list[str] = []
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        paths = [field.strip() for field in line.split("\t") if field.strip().startswith("/")]
        found.append(paths[-1] if paths else line.strip())
    return found


def widget_status(home: Path | None = None, uid: int | None = None) -> dict:
    _require_macos()
    if bundle.is_frozen():
        registrations = _appex_registrations()
        return {
            "bundled": True,
            "app": str(bundle.app_path()),
            "app_installed": True,
            "registered": bool(registrations),
            "registrations": registrations,
        }
    app = widget_app_path(home)
    state = launch_status(label=LABEL, uid=uid, home=home)
    state["app"] = str(app)
    state["app_installed"] = app.is_dir()
    return state
