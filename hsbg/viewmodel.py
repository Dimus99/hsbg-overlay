"""What the overlay draws — deliberately free of AppKit and of game internals.

Keeping this a plain data structure means the renderer can be exercised without
Hearthstone running (``tools/preview.py``) and the game logic can be tested
without a screen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Section keys used for collapsing and for hit-testing panel headers.
SECTION_LEADERBOARD = "leaderboard"
SECTION_STATS = "stats"
SECTION_HISTORY = "history"
SECTION_OPPONENTS = "opponents"
SECTION_POOL = "pool"
SECTION_HEROES = "heroes"

COLLAPSIBLE_SECTIONS = (SECTION_HEROES, SECTION_LEADERBOARD, SECTION_OPPONENTS,
                        SECTION_POOL, SECTION_STATS, SECTION_HISTORY)

# Panel headers, as i18n keys — the renderer resolves them against the
# language the game is running in (see :mod:`hsbg.i18n`).
SECTION_LABELS = {
    SECTION_LEADERBOARD: "section.leaderboard",
    SECTION_OPPONENTS: "section.opponents",
    SECTION_POOL: "section.pool",
    SECTION_STATS: "section.stats",
    SECTION_HISTORY: "section.history",
    SECTION_HEROES: "section.heroes",
}


@dataclass
class MinionView:
    card_id: str = ""
    name: str = ""
    attack: int = 0
    health: int = 0
    tier: int = 1
    golden: bool = False
    keywords: str = ""
    image: str = ""          # local path to the card render, "" while loading


@dataclass
class OddsView:
    headline: str = ""
    subtitle: str = ""
    win: float = 0.0
    tie: float = 0.0
    loss: float = 0.0
    avg_damage_dealt: float = 0.0
    avg_damage_taken: float = 0.0
    max_damage_taken: int = 0
    lethal_risk: float = 0.0
    coverage: float = 1.0
    unknown_cards: tuple[str, ...] = ()
    iterations: int = 0
    elapsed: float = 0.0
    margin: float = 0.0
    predicted: bool = False       # True when based on a remembered board
    stale_turns: int = 0
    known: bool = True            # False when we have never seen that board


@dataclass
class OpponentView:
    player_id: int = 0
    name: str = ""
    hero: str = ""
    tier: int = 1
    health: int = 0
    armor: int = 0
    place: int = 0
    turn_seen: int = 0
    dead: bool = False
    is_next: bool = False
    is_me: bool = False
    # True when we kept a board for this player, i.e. hovering the row is worth
    # something. Drives the small badge that advertises the popup.
    has_board: bool = False
    minions: list[MinionView] = field(default_factory=list)


@dataclass
class PoolEntry:
    card_id: str = ""
    name: str = ""
    tier: int = 1
    seen: int = 0
    remaining: int = 0
    total: int = 0


@dataclass
class HeroChoiceView:
    card_id: str = ""
    name: str = ""
    personal_avg: float = 0.0
    personal_games: int = 0
    global_avg: float = 0.0
    global_games: int = 0
    image: str = ""


@dataclass
class StatLine:
    label: str = ""
    value: str = ""
    detail: str = ""


@dataclass
class PopupView:
    """Floating card shown next to the cursor."""
    kind: str = ""                # "opponent" | "minion"
    title: str = ""
    subtitle: str = ""
    minions: list[MinionView] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    accent: str = "normal"        # normal | good | bad


@dataclass
class ViewModel:
    connected: bool = False
    # Empty until the app fills it in; the renderer falls back to "waiting for
    # Hearthstone", which is what an unfilled model means.
    status: str = ""
    turn: int = 0
    phase: str = "idle"
    my_health: int = 0
    my_armor: int = 0
    my_tier: int = 1
    odds: Optional[OddsView] = None
    # True while the fight is known but its odds are not yet: the board is
    # still being read, or the simulation is running. The panel shows a loader
    # instead of the previous fight's numbers.
    odds_pending: bool = False
    opponents: list[OpponentView] = field(default_factory=list)
    leaderboard: list[OpponentView] = field(default_factory=list)
    pool: list[PoolEntry] = field(default_factory=list)
    stats: list[StatLine] = field(default_factory=list)
    hero_choices: list[HeroChoiceView] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    # Hearthstone's locale, which is also the language of our own labels:
    # Russian for a Russian client, English for everything else. Empty means
    # English, the same fallback the rest of the app uses.
    language: str = ""

    # --- presentation state ---
    hidden: bool = False                                  # only the main bar shows
    # "?" pins over the game's own portrait rail. Off unless the player asks for
    # them, from the switch in the lobby table's header.
    show_marks: bool = False
    collapsed: dict[str, bool] = field(default_factory=dict)
    popup: Optional[PopupView] = None
    hover_key: Optional[str] = None       # panel hotspot under the cursor, if any

    def is_collapsed(self, section: str) -> bool:
        return bool(self.collapsed.get(section))

    @property
    def in_combat(self) -> bool:
        return self.phase == "combat"

    def opponent_by_id(self, player_id: int) -> Optional[OpponentView]:
        for opp in self.opponents:
            if opp.player_id == player_id:
                return opp
        return None
