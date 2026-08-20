"""Turns a :class:`~hsbg.viewmodel.ViewModel` into flat drawing operations.

Separated from the AppKit views so each window's size can be computed before
anything is drawn, and so the layout can be exercised headlessly.

Each builder returns ``(ops, (width, height), hotspots)`` where a hotspot is
``(key, x, y, w, h)`` — the regions the panel window hit-tests for clicks and
hover.

Op tuples:
    ("rect",  x, y, w, h, radius, rgba)
    ("text",  x, y, string, size, weight, rgba, max_width)
    ("bar",   x, y, w, h, [(fraction, rgba), ...])
    ("image", x, y, w, h, path)
"""
from __future__ import annotations

import math
from typing import Any

from ..i18n import Strings, strings
from ..viewmodel import (SECTION_HEROES, SECTION_HISTORY, SECTION_LABELS,
                         SECTION_LEADERBOARD, SECTION_OPPONENTS, SECTION_STATS,
                         PopupView, ViewModel)
from .hover import mark_layout

# --- palette ---------------------------------------------------------------

BG = (0.06, 0.07, 0.10, 0.93)
PANEL = (0.11, 0.13, 0.18, 0.96)
HEADER = (0.09, 0.10, 0.14, 0.98)
TEXT = (0.92, 0.94, 0.97, 1.0)
DIM = (0.62, 0.66, 0.74, 1.0)
FAINT = (0.45, 0.49, 0.57, 1.0)
WIN = (0.26, 0.79, 0.45, 1.0)
TIE = (0.62, 0.64, 0.70, 1.0)
LOSS = (0.90, 0.31, 0.34, 1.0)
WARN = (0.98, 0.72, 0.24, 1.0)
ACCENT = (0.38, 0.68, 1.00, 1.0)
GOLD = (0.95, 0.80, 0.35, 1.0)
BUTTON = (0.20, 0.23, 0.30, 1.0)
HOVER = (0.21, 0.25, 0.34, 1.0)

PAD = 10.0
GAP = 7.0
LINE = 15.0

PANEL_WIDTH = 288.0
BAR_WIDTH = 236.0
ODDS_WIDTH = 300.0
POPUP_WIDTH = 250.0

KEY_HIDE = "toggle-hide"
KEY_MARKS = "toggle-marks"


class Painter:
    def __init__(self, width: float):
        self.width = width
        self.ops: list[tuple[Any, ...]] = []
        self.hotspots: list[tuple[str, float, float, float, float]] = []
        self.y = 0.0

    def rect(self, x, y, w, h, radius, color) -> None:
        self.ops.append(("rect", x, y, w, h, radius, color))

    def text(self, x: float, y: float, s: str, size: float = 11.0,
             weight: str = "regular", color=TEXT, max_width: float = 0.0) -> None:
        if not s:
            return
        self.ops.append(("text", x, y, s, size, weight, color,
                         max_width or (self.width - x - PAD)))

    def bar(self, x, y, w, h, parts) -> None:
        self.ops.append(("bar", x, y, w, h, parts))

    def image(self, x, y, w, h, path: str) -> None:
        self.ops.append(("image", x, y, w, h, path))

    def hotspot(self, key: str, x, y, w, h) -> None:
        self.hotspots.append((key, x, y, w, h))

    def result(self, height: float):
        return self.ops, (self.width, height), self.hotspots


def _short(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------
# main bar — always visible, carries the hide toggle
# --------------------------------------------------------------------------

def build_main_bar(vm: ViewModel, width: float = BAR_WIDTH):
    t = strings(vm.language)
    p = Painter(width)
    height = 44.0
    p.rect(0, 0, width, height, 10.0, BG)

    dot = WARN if vm.in_combat else (WIN if vm.connected else FAINT)
    p.rect(11, 15, 7, 7, 3.5, dot)

    headline = vm.status or t("status.waiting_hs")
    if vm.in_combat and vm.odds_pending:
        headline = t("bar.combat_pending")
    elif vm.in_combat and vm.odds is not None and vm.odds.known:
        headline = t("bar.combat_win", win=f"{vm.odds.win:.0f}")
    p.text(25, 8, headline, 11.5, "semibold", TEXT, width - 25 - 48)

    detail = ""
    if vm.turn:
        life = f"{vm.my_health}" + (f"+{vm.my_armor}" if vm.my_armor else "")
        detail = t("bar.detail", turn=vm.turn, life=life, tier=vm.my_tier)
    p.text(25, 24, detail, 9.5, "regular", DIM, width - 25 - 48)

    # The one button: hide everything except this bar.
    bw, bh = 28.0, 20.0
    bx, by = width - bw - 10.0, 12.0
    p.rect(bx, by, bw, bh, 6.0, BUTTON)
    p.text(bx + bw / 2 - 5.0, by + 3.5, "▸" if vm.hidden else "▾",
           11.0, "bold", TEXT, 14.0)
    p.hotspot(KEY_HIDE, bx - 4, by - 6, bw + 8, bh + 12)

    return p.result(height)


# --------------------------------------------------------------------------
# odds — combat only, pinned to the top centre of the game window
# --------------------------------------------------------------------------

def build_odds(vm: ViewModel, width: float = ODDS_WIDTH, phase: float = 0.0):
    o = vm.odds
    t = strings(vm.language)
    p = Painter(width)
    if vm.odds_pending:
        return _build_odds_loading(p, t, o, phase)
    if o is None or not o.known:
        return p.result(0.0)

    extra = LINE if o.lethal_risk >= 1.0 else 0.0
    if o.coverage < 0.999:
        extra += LINE
    height = 78.0 + extra
    p.rect(0, 0, width, height, 10.0, BG)

    inner = width - 2 * PAD
    x = PAD
    p.text(x, 8, o.headline, 10.5, "semibold", TEXT, inner)

    total = max(1e-6, o.win + o.tie + o.loss)
    p.bar(x, 26, inner, 9.0, [(o.win / total, WIN), (o.tie / total, TIE),
                              (o.loss / total, LOSS)])

    y = 40.0
    p.text(x, y, f"{o.win:.0f}%", 16.0, "bold", WIN, inner * 0.3)
    p.text(x + inner * 0.34, y + 4, t("odds.tie", tie=f"{o.tie:.0f}"), 10.5, "regular",
           TIE, inner * 0.32)
    p.text(x + inner * 0.72, y, f"{o.loss:.0f}%", 16.0, "bold", LOSS, inner * 0.28)
    y += 22.0

    p.text(x, y, t("odds.damage", avg=f"{o.avg_damage_taken:.1f}",
                   max=o.max_damage_taken, margin=f"{o.margin:.1f}"),
           9.5, "regular", DIM, inner)
    y += LINE

    if o.lethal_risk >= 1.0:
        p.text(x, y, t("odds.lethal", risk=f"{o.lethal_risk:.0f}"), 10.5, "bold", LOSS, inner)
        y += LINE
    if o.coverage < 0.999:
        p.text(x, y, t("odds.coverage", coverage=f"{o.coverage:.0%}",
                       cards=_short(", ".join(o.unknown_cards[:2]), 32)),
               9.0, "regular", WARN, inner)
        y += LINE

    return p.result(height)


TRACK = (0.18, 0.20, 0.26, 1.0)
SHUTTLE_SHARE = 0.32       # how much of the track the moving segment covers


def _build_odds_loading(p: Painter, t: Strings, o, phase: float):
    """Placeholder card: the fight is on, the numbers are not in yet.

    Same width and anchor as the real card, so the headline stays put when the
    odds arrive. ``phase`` (0..1, supplied by the overlay's timer) drives an
    indeterminate slider — there is no honest progress to report, since the
    board is still being read out of the log.
    """
    width = p.width
    height = 56.0
    p.rect(0, 0, width, height, 10.0, BG)

    inner = width - 2 * PAD
    x = PAD
    p.text(x, 8, (o.headline if o is not None and o.headline else t("odds.combat")),
           10.5, "semibold", TEXT, inner)

    p.rect(x, 26, inner, 9.0, 4.5, TRACK)
    segment = inner * SHUTTLE_SHARE
    # Cosine ease so the slider drifts rather than ricochets off the ends.
    offset = (inner - segment) * (0.5 - 0.5 * math.cos(2.0 * math.pi * phase))
    p.rect(x + offset, 26, segment, 9.0, 4.5, ACCENT)

    p.text(x, 39, (o.subtitle if o is not None and o.subtitle else t("odds.pending")),
           9.5, "regular", FAINT, inner)
    return p.result(height)


# --------------------------------------------------------------------------
# hover pins — drawn on top of Hearthstone's own opponent rail
# --------------------------------------------------------------------------

def build_hero_marks(rows, width: float, height: float, slots: int = 8,
                     hover_index: int = -1):
    """A "?" pin per opponent portrait, so hovering is not a guessing game.

    Hearthstone logs no mouse-over, so the popup is triggered by geometry: a
    band of the window is split into slots. Nothing on screen says where those
    bands are, which left you sweeping the cursor to find them. The pins are
    drawn from the very same zone the hit test uses, so wherever a pin is, the
    hover works — and if the pins do not line up with the portraits, the zone
    needs calibrating and now you can see it.

    ``width``/``height`` are the rail's size in points; the caller positions
    the window over the game.
    """
    p = Painter(width)
    if not rows or slots <= 0 or width <= 2.0 or height <= 0:
        return p.result(0.0)

    # Same geometry the hit test uses, so a pin and its hotspot cannot drift.
    layout = mark_layout(width, height, slots)
    if layout is None:
        return p.result(0.0)
    x, size, slot_h = layout
    for i, row in enumerate(rows[:slots]):
        if row.is_me:
            continue
        y = slot_h * i + (slot_h - size) / 2.0
        active = i == hover_index
        # Dark disc behind it: the pin has to read over whatever art is under.
        p.rect(x - 1.5, y - 1.5, size + 3, size + 3, (size + 3) / 2,
               (0.03, 0.04, 0.06, 0.85))
        fill = ACCENT if row.has_board else (0.42, 0.46, 0.54, 1.0)
        if active:
            fill = GOLD
        p.rect(x, y, size, size, size / 2, fill)
        p.text(x + size * 0.30, y + size * 0.07, "?", size * 0.66, "bold",
               (0.05, 0.06, 0.09, 1.0), size)
    return p.result(height)


# --------------------------------------------------------------------------
# collapsible panels
# --------------------------------------------------------------------------

def build_panels(vm: ViewModel, width: float = PANEL_WIDTH):
    p = Painter(width)
    if vm.hidden:
        return p.result(0.0)

    sections = (
        # Hero picking happens first and is over in seconds — put it on top.
        (SECTION_HEROES, vm.hero_choices, _draw_hero_choices),
        (SECTION_LEADERBOARD, vm.leaderboard, _draw_leaderboard),
        (SECTION_OPPONENTS, vm.opponents, _draw_opponents),
        (SECTION_STATS, vm.stats, _draw_stats),
        (SECTION_HISTORY, vm.history, _draw_history),
    )
    drawn = False
    for key, data, drawer in sections:
        if not data:
            continue
        drawn = True
        _section(p, vm, key, data, drawer)

    if vm.warnings:
        _warnings(p, vm)
        drawn = True

    if not drawn:
        return p.result(0.0)

    height = p.y
    p.ops.insert(0, ("rect", 0.0, 0.0, width, height, 10.0, BG))
    return p.result(height)


def _section(p: Painter, vm: ViewModel, key: str, data, drawer) -> None:
    inner = p.width - 2 * PAD
    collapsed = vm.is_collapsed(key)
    header_h = 20.0
    # The pins are drawn over the game's own portrait rail, where there is
    # nothing of ours to click, so their switch rides the header of the list
    # that names those very players.
    switch = key == SECTION_LEADERBOARD
    reserved = (TOGGLE_W + 8.0) if switch else 0.0

    title = strings(vm.language)(SECTION_LABELS[key]).upper()
    p.rect(PAD, p.y, inner, header_h, 6.0, HEADER)
    p.text(PAD + 8, p.y + 4.5, ("▸ " if collapsed else "▾ ") + title,
           9.0, "bold", FAINT, inner - 40 - reserved)
    if switch:
        # Registered before the header's own hotspot, which covers the whole
        # row: the first match wins, so clicking the pill must not also
        # collapse the section.
        _marks_toggle(p, PAD + inner - TOGGLE_W - 4.0,
                      p.y + (header_h - TOGGLE_H) / 2.0, vm.show_marks,
                      strings(vm.language))
    p.text(PAD + inner - 28 - reserved, p.y + 4.5, str(len(data)), 9.0,
           "regular", FAINT, 22)
    p.hotspot(key, PAD, p.y, inner, header_h)
    p.y += header_h

    if collapsed:
        p.y += GAP
        return
    p.y += 3.0
    drawer(p, vm, data)
    p.y += GAP


# The pill switch in the lobby table's header.
TOGGLE_W = 46.0
TOGGLE_H = 14.0


def _marks_toggle(p: Painter, x: float, y: float, on: bool, t: Strings) -> None:
    """Switch for the "?" pins on Hearthstone's portrait rail.

    Labelled with the state it is in rather than the action it performs: a lit
    pill beside the list of opponents reads as "the pins are on" at a glance,
    and the pins themselves are the feedback for the click.
    """
    p.rect(x, y, TOGGLE_W, TOGGLE_H, TOGGLE_H / 2, ACCENT if on else BUTTON)
    p.text(x + 8.0, y + 2.0, t("marks.on") if on else t("marks.off"), 8.5, "bold",
           (0.05, 0.06, 0.09, 1.0) if on else DIM, TOGGLE_W - 10.0)
    p.hotspot(KEY_MARKS, x - 4.0, y - 4.0, TOGGLE_W + 8.0, TOGGLE_H + 8.0)


def _list_panel(p: Painter, count: int) -> tuple[float, float, float]:
    """Reserve a rounded box for ``count`` rows. Returns (x, first row y, inner)."""
    inner = p.width - 2 * PAD
    height = 6.0 + LINE * count + 4.0
    top = p.y
    p.rect(PAD, top, inner, height, 6.0, PANEL)
    p.y = top + height
    return PAD + 8, top + 6.0, inner


HERO_ROW = 30.0


def _draw_hero_choices(p: Painter, vm: ViewModel, rows) -> None:
    """One row per offered hero: portrait, name, and the placement data."""
    t = strings(vm.language)
    inner = p.width - 2 * PAD
    height = 6.0 + HERO_ROW * len(rows) + 4.0
    top = p.y
    p.rect(PAD, top, inner, height, 6.0, PANEL)
    for i, hero in enumerate(rows):
        ry = top + 6.0 + i * HERO_ROW
        x = PAD + 6
        if hero.image:
            p.image(x, ry, 20.0, 26.0, hero.image)
        p.text(x + 25, ry, _short(hero.name, 22), 10.0, "semibold", TEXT, inner - 110)
        if hero.personal_games:
            p.text(x + 25, ry + 13,
                   t("hero.personal", avg=f"{hero.personal_avg:.2f}",
                     games=t.plural(hero.personal_games, "games")),
                   8.5, "regular", DIM, inner - 40)
        else:
            p.text(x + 25, ry + 13, t("hero.unplayed"), 8.5, "regular", FAINT, inner - 40)
        if hero.global_games:
            p.text(PAD + inner - 62, ry + 3, f"{hero.global_avg:.2f}", 12.0, "bold",
                   ACCENT, 40)
            p.text(PAD + inner - 62, ry + 17, t("hero.global"), 8.0, "regular", FAINT, 56)
        elif hero.personal_games:
            p.text(PAD + inner - 62, ry + 3, f"{hero.personal_avg:.2f}", 12.0, "bold",
                   GOLD, 40)
            p.text(PAD + inner - 62, ry + 17, t("hero.yours"), 8.0, "regular", FAINT, 40)
    p.y = top + height


# Badge marking a row whose board we can show: three little minions in a line.
# Small on purpose — it is a hint, not a control.
BADGE_W = 13.5
BADGE_H = 10.0
HINT_LINE = 13.0


def _board_badge(p: Painter, x: float, y: float, active: bool) -> None:
    for i in range(3):
        p.rect(x + i * 5.0, y, 3.5, BADGE_H, 1.2, ACCENT if active else FAINT)


def _row_highlight(p: Painter, y: float, inner: float, active: bool) -> None:
    """Lit row under the cursor, so it is obvious which one the popup belongs to."""
    if active:
        p.rect(PAD + 2, y - 2, inner - 4, LINE, 4.0, HOVER)


def _draw_leaderboard(p: Painter, vm: ViewModel, rows) -> None:
    t = strings(vm.language)
    x, y0, inner = _list_panel(p, len(rows))
    for i, row in enumerate(rows):
        ry = y0 + i * LINE
        key = f"hero:{row.player_id}"
        active = not row.is_me and vm.hover_key == key
        _row_highlight(p, ry, inner, active)
        colour = GOLD if row.is_me else (FAINT if row.dead else TEXT)
        p.text(x, ry, f"{row.place or i + 1}.", 9.5, "regular", FAINT, 16)
        p.text(x + 16, ry, _short(row.name or row.hero, 18), 9.5,
               "semibold" if row.is_next else "regular",
               ACCENT if row.is_next else colour, inner * 0.46)
        hp = "—" if row.dead else f"{row.health}" + (f"+{row.armor}" if row.armor else "")
        p.text(x + inner * 0.56, ry, hp, 9.5, "regular", colour, 40)
        p.text(x + inner * 0.76, ry, t("row.tier", tier=row.tier), 9.5, "regular", FAINT, 24)
        if not row.is_me:
            # Hovering a row pops that opponent's last known board.
            if row.has_board:
                _board_badge(p, PAD + inner - 8 - BADGE_W, ry + 2.0,
                             active or row.is_next)
            p.hotspot(key, PAD, ry - 2, inner, LINE)


def _draw_opponents(p: Painter, vm: ViewModel, rows) -> None:
    """A compact index only — the boards themselves live in the hover popup.

    Which is invisible until you happen to point at a row, so every row we can
    actually show carries a badge, and the whole row lights up under the cursor.
    """
    t = strings(vm.language)
    inner = p.width - 2 * PAD
    hint = any(opp.has_board for opp in rows)
    height = 6.0 + LINE * len(rows) + (HINT_LINE if hint else 0.0) + 4.0
    top = p.y
    p.rect(PAD, top, inner, height, 6.0, PANEL)
    x, y0 = PAD + 8, top + 6.0

    for i, opp in enumerate(rows):
        ry = y0 + i * LINE
        key = f"hero:{opp.player_id}"
        active = vm.hover_key == key
        _row_highlight(p, ry, inner, active)
        title = ("▶ " if opp.is_next else "") + _short(opp.name or opp.hero, 20)
        p.text(x, ry, title, 9.5, "semibold" if opp.is_next else "regular",
               ACCENT if opp.is_next else TEXT, inner * 0.50)
        label = (t("row.board", minions=t.plural(len(opp.minions), "minions"),
                   turn=opp.turn_seen) if opp.minions else t("row.unseen"))
        p.text(x + inner * 0.53, ry, label, 9.0, "regular", FAINT, inner * 0.30)
        if opp.has_board:
            _board_badge(p, PAD + inner - 8 - BADGE_W, ry + 2.0, active or opp.is_next)
        p.hotspot(key, PAD, ry - 2, inner, LINE)

    if hint:
        p.text(x, y0 + LINE * len(rows) + 1.0, t("row.hover_hint"),
               8.5, "regular", FAINT, inner - 16)
    p.y = top + height


def _draw_stats(p: Painter, vm: ViewModel, rows) -> None:
    x, y0, inner = _list_panel(p, len(rows))
    for i, line in enumerate(rows):
        ry = y0 + i * LINE
        p.text(x, ry, _short(line.label, 24), 9.5, "regular", TEXT, inner * 0.58)
        p.text(x + inner * 0.60, ry, line.value, 9.5, "semibold", ACCENT, inner * 0.2)
        p.text(x + inner * 0.80, ry, line.detail, 9.0, "regular", FAINT, inner * 0.2)


def _draw_history(p: Painter, vm: ViewModel, rows) -> None:
    x, y0, inner = _list_panel(p, len(rows))
    for i, row in enumerate(rows):
        colour = WIN if row.startswith("W") else (LOSS if row.startswith("L") else TIE)
        p.text(x, y0 + i * LINE, row, 9.5, "regular", colour, inner - 16)


def _warnings(p: Painter, vm: ViewModel) -> None:
    inner = p.width - 2 * PAD
    height = 6.0 + LINE * len(vm.warnings) + 4.0
    top = p.y
    p.rect(PAD, top, inner, height, 6.0, (0.28, 0.20, 0.08, 0.96))
    for i, text in enumerate(vm.warnings):
        p.text(PAD + 8, top + 6.0 + i * LINE, "⚠︎ " + text, 9.5, "regular", WARN, inner - 16)
    p.y = top + height + GAP


# --------------------------------------------------------------------------
# hover popup
# --------------------------------------------------------------------------

def build_popup(popup: PopupView, width: float = POPUP_WIDTH):
    """Text popup for tavern minions, card renders for opponent boards."""
    if popup is not None and popup.minions:
        return _build_board_popup(popup)
    p = Painter(width)
    if popup is None:
        return p.result(0.0)

    inner = width - 2 * PAD
    height = 36.0 + len(popup.lines) * LINE + 8.0
    p.rect(0, 0, width, height, 9.0, BG)
    accent = {"good": WIN, "bad": LOSS}.get(popup.accent, ACCENT)
    p.text(PAD, 8, popup.title, 11.0, "semibold", accent, inner)
    p.text(PAD, 21, popup.subtitle, 9.0, "regular", FAINT, inner)
    for i, line in enumerate(popup.lines):
        p.text(PAD, 38.0 + i * LINE, line, 9.5, "regular", DIM, inner)
    return p.result(height)


# One card cell: square art on top, name strip under it, stats in the corners.
CARD_W = 64.0
CARD_H = 82.0
CARD_ART = 60.0
CARD_GAP = 4.0


def _build_board_popup(popup: PopupView):
    """The opponent's warband, drawn as cards.

    HearthstoneJSON only has framed renders for older sets, so we take the raw
    artwork — which exists for every card — and frame it here. That is also the
    only way the numbers can be right: a printed card shows its base stats,
    while what matters is what the minion actually had when the fight started.
    """
    count = len(popup.minions)
    width = max(2 * PAD + count * CARD_W + (count - 1) * CARD_GAP, 210.0)
    p = Painter(width)

    header = 34.0
    height = header + CARD_H + 8.0
    p.rect(0, 0, width, height, 9.0, BG)

    accent = {"good": WIN, "bad": LOSS}.get(popup.accent, ACCENT)
    p.text(PAD, 7, popup.title, 11.0, "semibold", accent, width - 2 * PAD)
    p.text(PAD, 20, popup.subtitle, 9.0, "regular", FAINT, width - 2 * PAD)

    for i, m in enumerate(popup.minions):
        _card(p, PAD + i * (CARD_W + CARD_GAP), header, m)

    return p.result(height)


def _card(p: Painter, x: float, y: float, m) -> None:
    frame = GOLD if m.golden else (0.24, 0.27, 0.34, 1.0)
    p.rect(x - 1, y - 1, CARD_W + 2, CARD_H + 2, 6.0, frame)
    p.rect(x, y, CARD_W, CARD_H, 5.0, (0.09, 0.10, 0.14, 1.0))

    if m.image:
        p.image(x + 2, y + 2, CARD_W - 4, CARD_ART, m.image)
    else:
        p.rect(x + 2, y + 2, CARD_W - 4, CARD_ART, 4.0, PANEL)
        p.text(x + 5, y + 6, "…", 10.0, "regular", FAINT, CARD_W - 10)

    # Name strip under the art. Cocoa truncates at the cell width, which fits
    # more of the name than a fixed character budget did.
    p.text(x + 3, y + CARD_ART + 4, m.name, 8.0, "regular", TEXT, CARD_W - 6)

    # Live stats, sitting on the bottom corners of the art.
    badge_y = y + CARD_ART - 15
    p.rect(x + 2, badge_y, 21, 15, 4.0, (0.05, 0.06, 0.09, 0.9))
    p.text(x + 5, badge_y + 1.5, str(m.attack), 10.5, "bold", (1.0, 0.85, 0.25, 1.0), 18)
    p.rect(x + CARD_W - 23, badge_y, 21, 15, 4.0, (0.05, 0.06, 0.09, 0.9))
    p.text(x + CARD_W - 20, badge_y + 1.5, str(m.health), 10.5, "bold",
           (1.0, 0.42, 0.38, 1.0), 18)

    if m.keywords:
        p.rect(x + 2, y + 2, CARD_W - 4, 13, 4.0, (0.05, 0.06, 0.09, 0.8))
        p.text(x + 5, y + 2.5, m.keywords, 9.0, "bold", WARN, CARD_W - 10)
