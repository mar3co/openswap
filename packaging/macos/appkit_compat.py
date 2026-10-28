"""Shared native-control compatibility for every frozen OpenSwap build."""

# AppKit chooses some control styles using the main executable's linked SDK.
# PyInstaller lowers that value to Python's SDK (a workaround for older Tk),
# so two Python distributions can otherwise produce different-looking apps.
# OpenSwap uses AppKit, not Tk. Keep the shipped 26.2 appearance baseline on
# both CPU architectures and in DEV. Change it deliberately with UI review.
# This is independent of the minimum macOS version; do not raise minos here.
APPKIT_SDK = (26, 2, 0)


def _require_single_arch(executable, osx):
    # PyInstaller's SDK getter/setter handles only the first slice.
    _fat, architectures = osx.get_binary_architectures(executable)
    if len(architectures) != 1 or architectures[0] not in {"arm64", "x86_64"}:
        raise RuntimeError("OpenSwap's AppKit policy requires one arm64 or x86_64 executable")


def check_appkit_sdk(executable, osx):
    """Fail the build if a later packaging step undid the shared SDK policy."""
    _require_single_arch(executable, osx)
    if osx.get_macos_sdk_version(executable) != APPKIT_SDK:
        raise RuntimeError(f"OpenSwap's AppKit SDK must be {APPKIT_SDK}: {executable}")


def normalize_appkit_sdk(executable, osx):
    """Apply after EXE, before COLLECT/BUNDLE and final distribution signing."""
    _require_single_arch(executable, osx)
    minimum = osx.macosx_version_min(executable)
    if osx.get_macos_sdk_version(executable) != APPKIT_SDK:
        osx.set_macos_sdk_version(executable, *APPKIT_SDK)
        # Editing a Mach-O header invalidates its signature. Repair it before
        # PyInstaller copies it into the bundle; build.sh signs releases later.
        osx.sign_binary(executable)
    check_appkit_sdk(executable, osx)
    if osx.macosx_version_min(executable) != minimum:
        raise RuntimeError("Applying OpenSwap's AppKit policy changed the minimum macOS version")


if __name__ == "__main__":
    import sys
    from PyInstaller.utils import osx

    if len(sys.argv) != 2:
        raise SystemExit("usage: appkit_compat.py /path/to/OpenSwap")
    check_appkit_sdk(sys.argv[1], osx)
    print("OpenSwap native appearance SDK: " + ".".join(map(str, APPKIT_SDK)))
