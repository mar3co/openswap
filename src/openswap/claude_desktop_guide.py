"""Short Desktop introduction using the same artwork as profile shortcuts."""

from pathlib import Path

from openswap.claude_desktop import APP_PATH, ClaudeDesktopError


def illustrations():
    original, profile = None, None
    try:
        import AppKit as A
        from openswap.claude_desktop_shortcut import _icon_bitmap, original_icon

        original = A.NSImage.alloc().initWithContentsOfFile_(str(original_icon(APP_PATH)))
        mark = A.NSImage.alloc().initWithContentsOfFile_(str(Path(__file__).parent / "assets/opensoft-symbol.svg"))
        if original is not None and original.isValid() and mark is not None and mark.isValid():
            profile = A.NSImage.alloc().initWithSize_((64, 64))
            profile.addRepresentation_(_icon_bitmap(original, mark, "1", "orange", 128))
    except (ImportError, ClaudeDesktopError, OSError):
        # The guide remains readable before Claude is installed.
        pass
    return [
        {"image": original, "title": "Your Claude icon", "running": original is not None,
         "body": "May open any running profile. For your original session, use Default → Open in OpenSwap."},
        {"image": profile, "title": "Your profile icon", "running": profile is not None,
         "body": "Claude + OpenSwap + a number. Opens only that profile. The dot means it’s running."},
    ]
