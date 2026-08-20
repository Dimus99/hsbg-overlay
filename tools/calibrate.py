#!/usr/bin/env python3
"""Where the cursor is inside the Hearthstone window, as a fraction.

Hearthstone does not log mouse-over, so the overlay decides what you are
pointing at from geometry. Two modes:

    calibrate                    — just print the fraction under the cursor
    calibrate --set leaderboard  — record a zone from its two corners

The zones live in settings.json as [left, top, right, bottom], fractions of the
game window:

    "extra": {"hover_zones": {"leaderboard": [0.072, 0.325, 0.158, 0.80],
                              "tavern":      [0.20, 0.215, 0.80, 0.42]}}
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import AppKit  # noqa: E402

from hsbg.config import Settings                          # noqa: E402
from hsbg.ui.hover import hit_test, zones_from_settings   # noqa: E402
from hsbg.ui.hswindow import find_hearthstone_window      # noqa: E402

ZONE_PROMPTS = {
    "leaderboard": ("the TOP-LEFT corner of the TOPMOST opponent portrait",
                    "the BOTTOM-RIGHT corner of the BOTTOM one"),
    "tavern": ("the TOP-LEFT corner of Bob's first card",
               "the BOTTOM-RIGHT corner of Bob's last card"),
}


def _fraction_now() -> tuple[float, float] | None:
    """Cursor as a fraction of the Hearthstone window, or None if it is gone."""
    rect = find_hearthstone_window()
    if rect is None or rect.width <= 0 or rect.height <= 0:
        return None
    location = AppKit.NSEvent.mouseLocation()
    screens = AppKit.NSScreen.screens()
    height = screens[0].frame().size.height if screens else 0
    return ((location.x - rect.x) / rect.width,
            (height - location.y - rect.y) / rect.height)


def capture(zone: str) -> int:
    """Record a zone from two cursor positions and save it."""
    first, second = ZONE_PROMPTS[zone]
    corners = []
    for prompt in (first, second):
        input(f"Point the cursor at {prompt} and press Enter here… ")
        point = _fraction_now()
        if point is None:
            print("No Hearthstone window — start the game and try again.")
            return 1
        print(f"  recorded x={point[0]:.3f} y={point[1]:.3f}")
        corners.append(point)

    left, right = sorted((corners[0][0], corners[1][0]))
    top, bottom = sorted((corners[0][1], corners[1][1]))
    if right - left < 0.01 or bottom - top < 0.01:
        print("The corners nearly coincide — the cursor did not move. Nothing written.")
        return 1

    settings = Settings.load()
    zones = dict(settings.extra.get("hover_zones") or {})
    zones[zone] = [round(v, 4) for v in (left, top, right, bottom)]
    settings.extra["hover_zones"] = zones
    settings.save()
    print(f"\nWrote {zone} = {zones[zone]}\nRestart the overlay.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", dest="zone", choices=sorted(ZONE_PROMPTS),
                    help="record a zone from its two corners instead of just showing it")
    args = ap.parse_args()
    if args.zone:
        return capture(args.zone)

    settings = Settings.load()
    zones = zones_from_settings(settings.extra)
    print("Move the mouse over the Hearthstone window. Ctrl+C to quit.\n")
    last = ""
    try:
        while True:
            rect = find_hearthstone_window()
            if rect is None:
                line = "no Hearthstone window"
            else:
                location = AppKit.NSEvent.mouseLocation()
                screens = AppKit.NSScreen.screens()
                height = screens[0].frame().size.height if screens else 0
                cursor = (location.x, height - location.y)
                fx = (cursor[0] - rect.x) / rect.width if rect.width else 0
                fy = (cursor[1] - rect.y) / rect.height if rect.height else 0
                hit = hit_test(cursor, rect, zones, tavern_slots=6)
                label = f"{hit.kind}[{hit.index}]" if hit else "—"
                line = f"x={fx:6.3f}  y={fy:6.3f}   zone: {label}"
            if line != last:
                print("\r" + line.ljust(60), end="", flush=True)
                last = line
            time.sleep(0.08)
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
