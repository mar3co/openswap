"""The DEV build's app icon: the release icon with a DEV banner."""

import subprocess
from pathlib import Path

ICONSET = {
    "icon_16x16.png": 16, "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32, "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128, "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256, "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512, "icon_512x512@2x.png": 1024,
}


def _badged(source, size):
    import AppKit as A

    rep = A.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, A.NSDeviceRGBColorSpace, 0, 0)
    A.NSGraphicsContext.saveGraphicsState()
    A.NSGraphicsContext.setCurrentContext_(A.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    source.drawInRect_(A.NSMakeRect(0, 0, size, size))
    # Below 32px the text is unreadable, so the banner alone marks it.
    banner = A.NSMakeRect(size * 0.08, size * 0.06, size * 0.84, size * 0.26)
    A.NSColor.systemOrangeColor().setFill()
    A.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(banner, size * 0.06, size * 0.06).fill()
    if size >= 32:
        font = A.NSFont.systemFontOfSize_weight_(size * 0.19, A.NSFontWeightHeavy)
        text = A.NSAttributedString.alloc().initWithString_attributes_(
            "DEV", {A.NSFontAttributeName: font, A.NSForegroundColorAttributeName: A.NSColor.whiteColor()})
        width, height = text.size()
        text.drawAtPoint_((banner.origin.x + (banner.size.width - width) / 2,
                           banner.origin.y + (banner.size.height - height) / 2))
    A.NSGraphicsContext.restoreGraphicsState()
    return rep.representationUsingType_properties_(A.NSBitmapImageFileTypePNG, {})


def make_dev_icon(source_png: Path, work: Path) -> Path:
    """Write AppIcon.icns under ``work`` from the 1024px release icon and return its path."""
    import AppKit as A

    source = A.NSImage.alloc().initWithContentsOfFile_(str(source_png))
    if source is None:
        raise ValueError(f"Cannot read the app icon: {source_png}")
    iconset = work / "AppIcon.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    for name, size in ICONSET.items():
        if not _badged(source, size).writeToFile_atomically_(str(iconset / name), True):
            raise OSError(f"Cannot write {iconset / name}")
    icns = work / "AppIcon.icns"
    subprocess.run(["iconutil", "-c", "icns", "-o", str(icns), str(iconset)], check=True)
    return icns
