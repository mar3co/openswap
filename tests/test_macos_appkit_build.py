"""Build-policy regressions: Python's SDK must not choose OpenSwap's tabs."""

from pathlib import Path
import plistlib
import runpy
import sys
from types import ModuleType, SimpleNamespace

import pytest


PACKAGING = Path(__file__).resolve().parents[1] / "packaging" / "macos"
POLICY = runpy.run_path(str(PACKAGING / "appkit_compat.py"))
SDK = POLICY["APPKIT_SDK"]


class MachO:
    """A freezer backend that records SDK/signature mutations per executable."""

    def __init__(self, sdk=(15, 0, 0), arches=("arm64",)):
        self.sdk = sdk
        self.arches = arches
        self.minimum = (11, 0, 0)
        self.calls = []

    def get_binary_architectures(self, path):
        return len(self.arches) > 1, list(self.arches)

    def get_macos_sdk_version(self, path):
        return self.sdk

    def macosx_version_min(self, path):
        return self.minimum

    def set_macos_sdk_version(self, path, *sdk):
        self.calls.append("edit")
        self.sdk = sdk

    def sign_binary(self, path):
        self.calls.append("sign")


@pytest.mark.parametrize("arch", ["arm64", "x86_64"])
@pytest.mark.parametrize("python_sdk", [(15, 0, 0), SDK, (27, 0, 0)])
def test_python_sdk_does_not_change_native_appearance(arch, python_sdk):
    binary = MachO(python_sdk, (arch,))
    POLICY["normalize_appkit_sdk"]("OpenSwap", binary)
    assert binary.sdk == SDK
    assert binary.minimum == (11, 0, 0)
    assert binary.calls == ([] if python_sdk == SDK else ["edit", "sign"])
    # Reusing an existing EXE on an incremental build is safe and idempotent.
    binary.calls.clear()
    POLICY["normalize_appkit_sdk"]("OpenSwap", binary)
    assert binary.calls == []


@pytest.mark.parametrize("arches", [(), ("arm64", "x86_64"), ("i386",)])
def test_rejects_an_executable_that_would_only_be_partially_patched(arches):
    binary = MachO(arches=arches)
    with pytest.raises(RuntimeError, match="one arm64 or x86_64"):
        POLICY["normalize_appkit_sdk"]("OpenSwap", binary)
    assert binary.calls == []


def test_failed_header_edit_cannot_silently_ship(monkeypatch):
    binary = MachO()
    monkeypatch.setattr(binary, "set_macos_sdk_version", lambda *_: None)
    with pytest.raises(RuntimeError, match="AppKit SDK must be"):
        POLICY["normalize_appkit_sdk"]("OpenSwap", binary)


def test_minimum_os_must_not_change(monkeypatch):
    binary = MachO()

    def edit(*_):
        binary.sdk = SDK
        binary.minimum = (26, 0, 0)

    monkeypatch.setattr(binary, "set_macos_sdk_version", edit)
    with pytest.raises(RuntimeError, match="minimum macOS version"):
        POLICY["normalize_appkit_sdk"]("OpenSwap", binary)


def test_signing_failure_stops_packaging(monkeypatch):
    binary = MachO()

    def fail(*_):
        raise RuntimeError("signing failed")

    monkeypatch.setattr(binary, "sign_binary", fail)
    with pytest.raises(RuntimeError, match="signing failed"):
        POLICY["normalize_appkit_sdk"]("OpenSwap", binary)


def execute_spec(monkeypatch, tmp_path, *, dev, python_sdk, bundled_sdk=None):
    """Execute the real spec with an EXE that models PyInstaller's SDK rewrite."""
    binary = MachO(python_sdk)
    pyinstaller, utils = ModuleType("PyInstaller"), ModuleType("PyInstaller.utils")
    utils.osx = binary
    pyinstaller.utils = utils
    monkeypatch.setitem(sys.modules, "PyInstaller", pyinstaller)
    monkeypatch.setitem(sys.modules, "PyInstaller.utils", utils)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delenv("OPENSWAP_DEV_ENTRY", raising=False)
    monkeypatch.delenv("OPENSWAP_BUILD_NUMBER", raising=False)
    monkeypatch.delenv("OPENSWAP_ARCH", raising=False)
    runner = tmp_path / "preview runner.py"
    runner.write_text("# DEV preview\n")
    if dev:
        monkeypatch.setenv("OPENSWAP_DEV_ENTRY", str(runner))
    captured = {}

    def analysis(entries, **kwargs):
        captured["entries"] = entries
        return SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])

    def collect(*args, **kwargs):
        assert binary.sdk == SDK, "SDK policy must run before COLLECT"
        assert binary.calls == ([] if python_sdk == SDK else ["edit", "sign"])

    def bundle(*args, **kwargs):
        captured.update(kwargs)
        if bundled_sdk:
            binary.sdk = bundled_sdk
        return SimpleNamespace(name=str(tmp_path / "OpenSwap.app"))

    runpy.run_path(str(PACKAGING / "openswap.spec"), init_globals={
        "SPECPATH": str(PACKAGING),
        "Analysis": analysis,
        "PYZ": lambda *_: None,
        "EXE": lambda *_, **kw: SimpleNamespace(name="OpenSwap"),
        "COLLECT": collect,
        "BUNDLE": bundle,
    })
    return captured


@pytest.mark.parametrize("python_sdk", [(15, 0, 0), SDK, (27, 0, 0)])
def test_dev_and_release_use_the_same_native_appearance(monkeypatch, tmp_path, python_sdk):
    release = execute_spec(monkeypatch, tmp_path, dev=False, python_sdk=python_sdk)
    dev = execute_spec(monkeypatch, tmp_path, dev=True, python_sdk=python_sdk)
    assert release["info_plist"] == dev["info_plist"]
    assert release["info_plist"]["LSMinimumSystemVersion"] == "14.0"
    assert release["bundle_identifier"] == "com.opensoft.openswap"
    assert dev["bundle_identifier"] == "com.opensoft.openswap.dev"
    assert release["entries"] == [str(PACKAGING / "entry.py")]
    assert dev["entries"] == [str(tmp_path / "preview runner.py")]
    assert release["info_plist"]["CFBundleURLTypes"] == [{
        "CFBundleURLName": "Claude profile sign-in", "CFBundleURLSchemes": ["claude"],
        "CFBundleTypeRole": "Viewer", "LSHandlerRank": "Alternate"}]


def test_the_app_may_send_apple_events_and_says_why(monkeypatch, tmp_path):
    """Sign-in delivery is an Apple event to Claude: the hardened runtime needs
    the entitlement, and the consent prompt shows the usage description."""
    release = execute_spec(monkeypatch, tmp_path, dev=False, python_sdk=SDK)
    assert release["info_plist"]["NSAppleEventsUsageDescription"] == (
        "OpenSwap delivers Claude sign-in links to the Claude profile you choose.")
    entitlements = plistlib.loads((PACKAGING / "entitlements.plist").read_bytes())
    assert entitlements["com.apple.security.automation.apple-events"] is True


@pytest.mark.parametrize("dev", [False, True])
def test_finished_bundle_is_checked_too(monkeypatch, tmp_path, dev):
    with pytest.raises(RuntimeError, match="AppKit SDK must be"):
        execute_spec(monkeypatch, tmp_path, dev=dev, python_sdk=(15, 0, 0), bundled_sdk=(15, 0, 0))


def test_distribution_build_cannot_accidentally_use_local_runner():
    build = (PACKAGING / "build.sh").read_text()
    assert build.index("unset OPENSWAP_DEV_ENTRY") < build.index("pyinstaller --noconfirm")
    workflow = (PACKAGING.parents[1] / ".github/workflows/macos-app.yml").read_text()
    assert 'appkit_compat.py "$EXE"' in workflow
