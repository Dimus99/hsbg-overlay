"""Locating the Hearthstone window so the overlay can pin itself to it.

Uses the public window list, which exposes owner name and bounds without the
Screen Recording permission (only window *titles* are gated, and we do not need
those).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import AppKit
import Quartz

HEARTHSTONE_OWNERS = ("Hearthstone",)


@dataclass
class WindowRect:
    x: float
    y: float
    width: float
    height: float

    @property
    def area(self) -> float:
        return self.width * self.height


def find_hearthstone_window() -> Optional[WindowRect]:
    """Largest window owned by Hearthstone, in Quartz (top-left) space.

    A game in exclusive fullscreen lives on its own Space and drops out of the
    on-screen list whenever that Space is not active, so fall back to the full
    list rather than losing track of the window entirely.
    """
    rect = _search(Quartz.kCGWindowListOptionOnScreenOnly
                   | Quartz.kCGWindowListExcludeDesktopElements)
    if rect is None:
        rect = _search(Quartz.kCGWindowListOptionAll
                       | Quartz.kCGWindowListExcludeDesktopElements)
    return rect


def _search(options: int) -> Optional[WindowRect]:
    infos = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
    best: Optional[WindowRect] = None
    for info in infos:
        owner = info.get(Quartz.kCGWindowOwnerName) or ""
        if owner not in HEARTHSTONE_OWNERS:
            continue
        if int(info.get(Quartz.kCGWindowLayer, 0)) != 0:
            continue
        bounds = info.get(Quartz.kCGWindowBounds) or {}
        rect = WindowRect(float(bounds.get("X", 0)), float(bounds.get("Y", 0)),
                          float(bounds.get("Width", 0)), float(bounds.get("Height", 0)))
        if rect.width < 200 or rect.height < 200:
            continue
        if best is None or rect.area > best.area:
            best = rect
    return best


def hearthstone_is_running() -> bool:
    for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
        if (app.localizedName() or "") in HEARTHSTONE_OWNERS:
            return True
    return False


def hearthstone_is_frontmost() -> bool:
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    return app is not None and (app.localizedName() or "") in HEARTHSTONE_OWNERS


def quartz_to_cocoa(rect: WindowRect) -> tuple[float, float, float, float]:
    """Quartz uses a top-left origin on the primary screen; Cocoa uses bottom-left."""
    screens = AppKit.NSScreen.screens()
    if not screens:
        return rect.x, rect.y, rect.width, rect.height
    primary_height = screens[0].frame().size.height
    cocoa_y = primary_height - (rect.y + rect.height)
    return rect.x, cocoa_y, rect.width, rect.height
