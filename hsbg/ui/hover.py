"""Working out what the cursor is pointing at inside the Hearthstone window.

Hearthstone does not log mouse-over, so the only way to react to hovering a hero
portrait or a tavern minion is geometry: take the cursor position, express it
relative to the game window, and see which zone it falls in.

The zones are stored as fractions of the window, so they survive a resize, but
they are still estimates of Blizzard's layout. ``tools/calibrate.py`` prints the
fraction under the cursor so they can be corrected in settings.json without
touching code.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .hswindow import WindowRect

# Fractions of the Hearthstone window: (left, top, right, bottom).
DEFAULT_ZONES = {
    # The opposing-players rail down the left edge during the shop phase.
    #
    # Measured off a 1440x900 screenshot: the dark backdrop runs out at 0.070,
    # the portraits occupy 0.075-0.16, and the board's wood takes over past that.
    # Vertically the red portrait frames repeat every 0.0866 of the height, and
    # the place badges beside them (1..8, one per player, yourself included)
    # put the first tile's top at 0.154 and the last one's bottom at 0.847.
    # The old values (0, 0.09, 0.085, 0.96) sat left of the rail entirely, so
    # pointing at a portrait did nothing and only the empty strip beside it
    # answered — with the wrong player, since the pitch was off by a third.
    "leaderboard": (0.072, 0.154, 0.163, 0.847),
    # Bob's row of minions, roughly the upper-middle third of the board.
    "tavern": (0.200, 0.215, 0.800, 0.420),
}
LEADERBOARD_SLOTS = 8

# The "?" pin drawn over each opponent portrait, in points. Kept here rather
# than in the renderer because the hit test and the drawing have to agree
# exactly: the pin *is* the hotspot, so a mismatch either shows a popup where
# nothing is drawn or draws a pin that does not answer.
MARK_SIZE = 18.0        # diameter of one pin
MARK_MIN = 9.0          # below this it is unreadable, so nothing is drawn
MARK_INSET = 2.0        # gap between the pin and the right edge of the rail
MARK_PAD = 3.0          # forgiveness around the pin, so it is not pixel-hunting


@dataclass
class Hit:
    kind: str      # "hero" | "tavern"
    index: int     # slot index, 0-based from the top / from the left


def mark_layout(width: float, height: float, slots: int = LEADERBOARD_SLOTS):
    """Pin geometry inside the rail: ``(x, size, slot_height)`` or ``None``.

    ``width``/``height`` are the rail's size in points. Returns ``None`` when
    the rail is too small for a readable pin — in which case none is drawn and
    none can be pointed at either.
    """
    if slots <= 0 or width <= 0 or height <= 0:
        return None
    slot_h = height / slots
    size = min(MARK_SIZE, slot_h - 2.0, width - 2 * MARK_INSET)
    if size < MARK_MIN:
        return None
    return width - size - MARK_INSET, size, slot_h


def _fraction(cursor: tuple[float, float], rect: WindowRect
              ) -> Optional[tuple[float, float]]:
    """Cursor as a fraction of the window, or None when outside it."""
    if rect.width <= 0 or rect.height <= 0:
        return None
    fx = (cursor[0] - rect.x) / rect.width
    fy = (cursor[1] - rect.y) / rect.height
    if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
        return None
    return fx, fy


def _mark_hit(fx: float, fy: float, rect: WindowRect, zone: tuple,
              slots: int) -> Optional[Hit]:
    """Is the cursor on one of the "?" pins? Slot index if so."""
    left, top, right, bottom = zone
    width, height = (right - left) * rect.width, (bottom - top) * rect.height
    layout = mark_layout(width, height, slots)
    if layout is None:
        return None
    mark_x, size, slot_h = layout
    # Cursor inside the rail, in the same points the pins are laid out in.
    x = (fx - left) * rect.width
    y = (fy - top) * rect.height
    if not (mark_x - MARK_PAD <= x <= mark_x + size + MARK_PAD):
        return None
    slot = int(y // slot_h)
    if not 0 <= slot < slots:
        return None
    pin_top = slot * slot_h + (slot_h - size) / 2.0
    if not (pin_top - MARK_PAD <= y <= pin_top + size + MARK_PAD):
        return None
    return Hit("hero", slot)


def hit_test(cursor: tuple[float, float], rect: WindowRect, zones: dict,
             tavern_slots: int = 0, leaderboard_slots: int = LEADERBOARD_SLOTS,
             marks_enabled: bool = False) -> Optional[Hit]:
    """Which in-game element the cursor is over, if any.

    ``cursor`` and ``rect`` must both be in Quartz coordinates (top-left origin).

    The opponent rail only answers when ``marks_enabled`` — and then only over
    the "?" pin itself, not over the whole band. The band spans the portraits,
    so treating all of it as a hotspot popped a stale board open whenever the
    cursor merely passed left of the rail on its way somewhere else. The pin is
    the only thing on screen that advertises the popup, so it is the only thing
    that should trigger it; our own panel rows do the rest.
    """
    point = _fraction(cursor, rect)
    if point is None:
        return None
    fx, fy = point

    left, top, right, bottom = zones.get("leaderboard", DEFAULT_ZONES["leaderboard"])
    if marks_enabled and leaderboard_slots > 0 and left <= fx <= right and top <= fy <= bottom:
        hit = _mark_hit(fx, fy, rect, (left, top, right, bottom), leaderboard_slots)
        if hit is not None:
            return hit
        return None

    if tavern_slots > 0:
        left, top, right, bottom = zones.get("tavern", DEFAULT_ZONES["tavern"])
        if left <= fx <= right and top <= fy <= bottom:
            # Bob centres the row, so the occupied span narrows with fewer cards.
            span = (right - left) * tavern_slots / 7.0
            start = left + ((right - left) - span) / 2.0
            if start <= fx <= start + span:
                slot = int((fx - start) / max(1e-6, span) * tavern_slots)
                return Hit("tavern", min(slot, tavern_slots - 1))
    return None


def zones_from_settings(extra: dict) -> dict:
    """Merge any user-calibrated zones over the defaults."""
    zones = dict(DEFAULT_ZONES)
    custom = extra.get("hover_zones") or {}
    for key, value in custom.items():
        if key in zones and isinstance(value, (list, tuple)) and len(value) == 4:
            try:
                zones[key] = tuple(float(v) for v in value)
            except (TypeError, ValueError):
                continue
    return zones
