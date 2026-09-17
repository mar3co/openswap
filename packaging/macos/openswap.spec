# PyInstaller spec: one executable that is the menu bar extra when launched
# with no arguments and the CLI otherwise (see openswap.cli.main).
# Analysis/PYZ/EXE/COLLECT/BUNDLE are injected when PyInstaller execs this file.

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
        "CFBundleShortVersionString": "0.1.0",
        "LSUIElement": True,
        "LSMinimumSystemVersion": "14.0",
        "NSHumanReadableCopyright": "",
        "LSApplicationCategoryType": "public.app-category.developer-tools",
    },
)
