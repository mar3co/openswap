# PyInstaller spec: one executable that is the menu bar extra when launched
# with no arguments and the CLI otherwise (see openswap.cli.main).
# Analysis/PYZ/EXE/COLLECT/BUNDLE are injected when PyInstaller execs this file.
#
# The version comes from pyproject.toml, the one place it is written. build.sh
# passes the same string to xcodebuild so the appex matches its parent app,
# and OPENSWAP_BUILD_NUMBER (the CI run number) becomes CFBundleVersion.

import os
import tomllib
from pathlib import Path

with open(Path(SPECPATH, "..", "..", "pyproject.toml"), "rb") as f:
    VERSION = tomllib.load(f)["project"]["version"]
BUILD_NUMBER = os.environ.get("OPENSWAP_BUILD_NUMBER", "1")

a = Analysis(
    ["entry.py"],
    pathex=["../../src"],
    hiddenimports=["rumps", "AppKit", "Foundation", "objc", "truststore"],
    datas=[("../../src/openswap/assets", "openswap/assets")],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="OpenSwap",
    console=False,
    argv_emulation=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="OpenSwap")
app = BUNDLE(
    coll,
    name="OpenSwap.app",
    bundle_identifier="com.opensoft.openswap",
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
