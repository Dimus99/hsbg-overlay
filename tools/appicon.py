"""Render the launcher icon into an .iconset directory.

    ./.venv/bin/python tools/appicon.py build/hsbg.iconset

Called by make_app.sh; the palette matches the overlay (hsbg/ui/render.py).
"""
from __future__ import annotations

import os
import sys

import AppKit
from Foundation import NSMakePoint, NSMakeRect

PANEL = (0.13, 0.16, 0.22, 1.0)
DEEP = (0.05, 0.06, 0.09, 1.0)
EDGE = (0.35, 0.40, 0.50, 0.55)
ACCENT = (0.38, 0.68, 1.00, 1.0)
GOLD = (0.95, 0.80, 0.35, 1.0)

# iconutil wants exactly these files; the value is the pixel size to render.
SIZES = {
    "icon_16x16.png": 16, "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32, "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128, "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256, "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512, "icon_512x512@2x.png": 1024,
}


def _colour(rgba):
    return AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(*rgba)


def _draw(size: float) -> None:
    inset = size * 0.085
    rect = NSMakeRect(inset, inset, size - 2 * inset, size - 2 * inset)
    radius = rect.size.width * 0.225
    plate = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        rect, radius, radius)

    gradient = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
        _colour(PANEL), _colour(DEEP))
    gradient.drawInBezierPath_angle_(plate, -90.0)

    _colour(EDGE).setStroke()
    plate.setLineWidth_(max(1.0, size * 0.007))
    plate.stroke()

    font = AppKit.NSFont.systemFontOfSize_weight_(size * 0.40,
                                                 AppKit.NSFontWeightHeavy)
    label = AppKit.NSAttributedString.alloc().initWithString_attributes_("BG", {
        AppKit.NSFontAttributeName: font,
        AppKit.NSForegroundColorAttributeName: _colour(ACCENT),
        AppKit.NSKernAttributeName: -size * 0.012,
    })
    extent = label.size()
    label.drawAtPoint_(NSMakePoint((size - extent.width) / 2.0,
                                   (size - extent.height) / 2.0 + size * 0.055))

    # Accent bar under the wordmark, echoing the overlay's odds strip.
    bar_w, bar_h = size * 0.34, size * 0.045
    bar = NSMakeRect((size - bar_w) / 2.0, size * 0.235, bar_w, bar_h)
    _colour(GOLD).setFill()
    AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        bar, bar_h / 2.0, bar_h / 2.0).fill()


def render(size: int, path: str) -> None:
    rep = AppKit.NSBitmapImageRep.alloc()
    rep = rep.initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, AppKit.NSCalibratedRGBColorSpace, 0, 0)
    context = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(context)
    try:
        _draw(float(size))
    finally:
        context.flushGraphics()
        AppKit.NSGraphicsContext.restoreGraphicsState()

    data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    if not data.writeToFile_atomically_(path, True):
        raise RuntimeError(f"could not write {path}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: appicon.py <path>.iconset", file=sys.stderr)
        return 2
    target = argv[1]
    os.makedirs(target, exist_ok=True)
    for name, size in SIZES.items():
        render(size, os.path.join(target, name))
    print(f"icons written: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
