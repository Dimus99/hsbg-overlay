"""Per-fight journal of what we predicted against what actually happened.

Off unless switched on from the menu bar. When on, every combat that gets a
real outcome appends one JSON line holding the prediction, the result, and both
boards in full — enough to replay the fight offline without the original
Power.log, which is tens of millions of lines and rotates away.

The point is the disagreements, and one shape above all: a fight called at 0%
that is then won. That cannot be variance — 2000 runs found no path to a win, so
the board in the simulator was not the board in the game. Each row therefore
carries a ``surprise`` field naming how badly it missed, and
``tools/debugreview.py`` ranks by it.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from .config import DEBUG_DIR

if TYPE_CHECKING:
    from .gamestate import BoardSnapshot, CombatRecord

# How wrong a call has to be before it is worth a second look. A fight given
# under 2% that is not lost is the flagship case: no sampling noise explains it.
SURPRISE_THRESHOLD = 2.0


def _minion(m) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": m.entity_id, "card": m.card_id, "name": m.name,
        "atk": m.attack, "hp": m.health, "tier": m.tier, "pos": m.position,
    }
    if m.golden:
        out["golden"] = True
    if m.races:
        out["races"] = sorted(m.races)
    for flag in ("taunt", "divine_shield", "poisonous", "venomous", "reborn",
                 "windfury", "mega_windfury", "stealth", "frozen", "cant_attack"):
        if getattr(m, flag, False):
            out[flag] = True
    if any(m.script_data):
        out["script_data"] = list(m.script_data)
    return out


def _board(b: "BoardSnapshot") -> dict[str, Any]:
    """One side of a fight, complete enough to rebuild the simulator's input.

    Everything ``board_from_snapshot`` reads has to be here, or an offline
    replay quietly simulates a *different* fight and its disagreement with the
    live number reads as a bug in the engine. Health and armour decide the
    lethal-risk figure; the hero power and the start-of-combat flag decide
    whether the engine replays effects the game has already applied.
    """
    return {
        "player_id": b.player_id,
        "name": b.player_name,
        "hero": b.hero_card_id,
        "hero_power": b.hero_power_card_id,
        "health": b.hero_health,
        "armor": b.hero_armor,
        "tier": b.tech_level,
        "turn": b.turn,
        "post_start_of_combat": b.post_start_of_combat,
        "trinkets": list(b.trinkets),
        "minions": [_minion(m) for m in sorted(b.minions, key=lambda m: m.position)],
        "hand": [_minion(m) for m in b.hand],
    }


def surprise_of(prediction: Optional[dict], result: str) -> float:
    """How much probability the prediction put on anything but what happened.

    100 means it was certain of something else; 0 means it called it outright.
    """
    if not prediction or not result:
        return 0.0
    got = float(prediction.get(result, 0.0))
    return max(0.0, 100.0 - got)


class DebugJournal:
    """Append-only JSONL, one file per day."""

    def __init__(self, directory: Path = DEBUG_DIR):
        self.directory = directory
        self.error: str = ""

    def path_for(self, when: Optional[float] = None) -> Path:
        stamp = datetime.fromtimestamp(when or time.time()).strftime("%Y-%m-%d")
        return self.directory / f"combats-{stamp}.jsonl"

    def record(self, combat: "CombatRecord") -> Optional[Path]:
        """Write one finished fight. Returns the file, or None if it was skipped.

        Never raises: a broken journal must not take a live overlay down with it.
        """
        if not combat.actual_result:
            return None
        prediction = combat.prediction or None
        row = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "turn": combat.turn,
            "opponent_player_id": combat.opponent_player_id,
            "damage_cap": combat.damage_cap,
            "prediction": prediction,
            "actual": {
                "result": combat.actual_result,
                "damage": combat.actual_damage,
                # Whether the game stated the outcome outright or we inferred it
                # from the health bars. An inferred label can itself be wrong, so
                # a "surprise" on one of those is worth less.
                "from_log": combat.result_from_log,
                "attacks": combat.attacks,
            },
            "surprise": round(surprise_of(prediction, combat.actual_result), 1),
            "my_board": _board(combat.my_board),
            "opponent_board": _board(combat.opponent_board),
        }
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.path_for()
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.error = ""
            return path
        except OSError as exc:
            self.error = str(exc)
            return None


def read(directory: Path = DEBUG_DIR) -> list[dict[str, Any]]:
    """Every recorded fight, oldest first. Bad lines are skipped, not fatal."""
    rows: list[dict[str, Any]] = []
    if not directory.is_dir():
        return rows
    for path in sorted(directory.glob("combats-*.jsonl")):
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            row["_file"] = path.name
            rows.append(row)
    return rows
