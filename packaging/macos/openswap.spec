# PyInstaller spec: one executable that is the menu bar extra when launched
# with no arguments and the CLI otherwise (see openswap.cli.main).
# Analysis/PYZ/EXE/COLLECT/BUNDLE are injected when PyInstaller execs this file.
#
# The version comes from pyproject.toml, the one place it is written. build.sh
# passes the same string to xcodebuild so the appex matches its parent app,
# and OPENSWAP_BUILD_NUMBER (the CI run number) becomes CFBundleVersion.

import os
import sys
import tomllib
from pathlib import Path
from PyInstaller.utils import osx

HERE = Path(SPECPATH).resolve()
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
from appkit_compat import check_appkit_sdk, normalize_appkit_sdk

with open(ROOT / "pyproject.toml", "rb") as f:
    VERSION = tomllib.load(f)["project"]["version"]
BUILD_NUMBER = os.environ.get("OPENSWAP_BUILD_NUMBER", "1")
# build.sh sets this to arm64 or x86_64. PyInstaller fails rather than freeze
# an arch other than the one the release zip is named for; None means native.
TARGET_ARCH = os.environ.get("OPENSWAP_ARCH") or None
# Local test runners may label the menu bar DEV and skip installation setup.
# They must use this same spec, SDK policy, and plist as the release build.
DEV_ENTRY = os.environ.get("OPENSWAP_DEV_ENTRY")
ENTRY = Path(DEV_ENTRY).expanduser().resolve() if DEV_ENTRY else HERE / "entry.py"
if not ENTRY.is_file():
    raise ValueError(f"OpenSwap entry point does not exist: {ENTRY}")

a = Analysis(
    [str(ENTRY)],
    pathex=[str(ROOT / "src")],
    hiddenimports=["rumps", "AppKit", "Foundation", "objc", "truststore"],
    datas=[(str(ROOT / "src/openswap/assets"), "openswap/assets")],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="OpenSwap",
    console=False,
    argv_emulation=False,
    target_arch=TARGET_ARCH,
)
normalize_appkit_sdk(exe.name, osx)
coll = COLLECT(exe, a.binaries, a.datas, name="OpenSwap")
app = BUNDLE(
    coll,
    name="OpenSwap.app",
    bundle_identifier="com.opensoft.openswap.dev" if DEV_ENTRY else "com.opensoft.openswap",
    info_plist={
        "CFBundleDisplayName": "OpenSwap",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": BUILD_NUMBER,
        "LSUIElement": True,
        "LSMinimumSystemVersion": "14.0",
        "NSHumanReadableCopyright": "",
        "LSApplicationCategoryType": "public.app-category.developer-tools",
    },
)
check_appkit_sdk(str(Path(app.name) / "Contents" / "MacOS" / "OpenSwap"), osx)
