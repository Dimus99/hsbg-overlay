"""Minion pool tracking.

Every player in the lobby draws from one shared pool, so copies you have seen on
other boards are copies Bob cannot offer you. We can only count what we have
actually observed (our own board and hand, plus every opponent board we have
fought), which is a lower bound on what is gone — exactly the information that
makes a "should I keep rolling for this?" call.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .carddb import CardDB
    from .gamestate import BattlegroundsState

# Copies of each minion in the shared pool, by tavern tier. These are
# patch-dependent; adjust here if Blizzard changes them.
POOL_SIZE_BY_TIER = {1: 18, 2: 15, 3: 13, 4: 11, 5: 9, 6: 7, 7: 7}


@dataclass
class PoolCount:
    card_id: str
    name: str
    tier: int
    seen: int
    total: int

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.seen)


def count_seen(state: "BattlegroundsState") -> dict[str, tuple[int, int]]:
    """card id -> (copies seen, tier), summed over every board we know about."""
    counts: dict[str, tuple[int, int]] = {}

    def add(card_id: str, tier: int, copies: int) -> None:
        seen, known_tier = counts.get(card_id, (0, tier))
        counts[card_id] = (seen + copies, known_tier or tier)

    boards = list(state.last_boards.values())
    boards.append(state.current_my_board())
    for board in boards:
        for minion in board.minions:
            # A golden minion is three copies out of the pool.
            add(minion.base_card_id, minion.tier, 3 if minion.golden else 1)
    return counts


def build(state: "BattlegroundsState", db: Optional["CardDB"] = None,
          limit: int = 8, min_tier: int = 1) -> list[PoolCount]:
    """The entries worth showing: whatever is closest to being exhausted."""
    out: list[PoolCount] = []
    for card_id, (seen, tier) in count_seen(state).items():
        if tier < min_tier or tier not in POOL_SIZE_BY_TIER:
            continue
        total = POOL_SIZE_BY_TIER[tier]
        name = db.name(card_id) if db is not None and db.loaded else card_id
        out.append(PoolCount(card_id=card_id, name=name, tier=tier,
                             seen=min(seen, total), total=total))
    # Fewest left first — that is the actionable end of the list.
    out.sort(key=lambda p: (p.remaining, -p.tier))
    return out[:limit]
