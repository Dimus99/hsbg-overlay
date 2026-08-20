"""Value types for the combat simulator.

Deliberately slim and mutable: a single prediction copies these thousands of
times, so they use ``__slots__`` and hand-written clones rather than dataclass
machinery or ``copy.deepcopy``.
"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..gamestate import BoardSnapshot, MinionSnapshot


class SimMinion:
    __slots__ = (
        "card_id", "base_card_id", "name", "attack", "health", "max_health", "tier",
        "golden", "races", "taunt", "divine_shield", "poisonous", "venomous", "reborn",
        "windfury", "mega_windfury", "cleave", "stealth", "immune", "cant_attack",
        "attacks_taken", "dead", "uid", "summoned_in_combat", "script_data", "avenge_counter",
        "damaged_count", "kill_count", "killer", "extra_deathrattles", "entity_id",
    )

    def __init__(self, card_id: str = "", name: str = "", attack: int = 0, health: int = 1,
                 tier: int = 1, golden: bool = False, races: frozenset = frozenset()):
        self.card_id = card_id
        self.base_card_id = card_id[:-2] if card_id.endswith("_G") else card_id
        self.name = name
        self.attack = attack
        self.health = health
        self.max_health = health
        self.tier = tier
        self.golden = golden
        self.races = races
        self.taunt = False
        self.divine_shield = False
        self.poisonous = False
        self.venomous = False
        self.reborn = False
        self.windfury = False
        self.mega_windfury = False
        self.cleave = False
        self.stealth = False
        self.immune = False
        self.cant_attack = False
        self.attacks_taken = 0
        self.dead = False
        self.uid = 0
        self.summoned_in_combat = False
        self.script_data = (0, 0, 0)
        self.avenge_counter = 0
        # Per-combat trigger bookkeeping: Frenzy fires on the first survived hit,
        # "kills a minion" triggers need a counter, and a few deathrattles act on
        # whoever landed the killing blow.
        self.damaged_count = 0
        self.kill_count = 0
        self.killer: Optional["SimMinion"] = None
        # Deathrattles picked up from allies that died beside this minion
        # (Fish of N'Zoth). A tuple, so ``clone`` stays a flat slot copy.
        self.extra_deathrattles: tuple = ()
        # The log's id for this body, when it came from a snapshot. Only
        # tools/divergence.py uses it, to line a simulated minion up with the
        # one the game was actually swinging.
        self.entity_id = 0

    # ------------------------------------------------------------------

    def clone(self) -> "SimMinion":
        m = SimMinion.__new__(SimMinion)
        for slot in SimMinion.__slots__:
            setattr(m, slot, getattr(self, slot))
        return m

    def is_race(self, race: str) -> bool:
        return race in self.races or "ALL" in self.races

    @property
    def attacks_per_turn(self) -> int:
        if self.mega_windfury:
            return 4
        if self.windfury:
            return 2
        return 1

    @property
    def can_attack(self) -> bool:
        return not self.dead and self.attack > 0 and not self.cant_attack

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        kw = "".join(c for c, on in (
            ("T", self.taunt), ("D", self.divine_shield), ("P", self.poisonous),
            ("V", self.venomous), ("R", self.reborn), ("W", self.windfury)) if on)
        return f"<{self.name or self.card_id} {self.attack}/{self.health}{' ' + kw if kw else ''}>"


class SimBoard:
    __slots__ = ("minions", "hero_card_id", "hero_power_card_id", "tier", "player_id",
                 "attack_index", "hero_health", "hero_armor", "trinkets",
                 "deaths_this_combat", "graveyard", "post_start_of_combat", "hand",
                 "summon_buff", "hero_power", "space_queue", "trinket_avenge",
                 "token_buff")

    MAX_MINIONS = 7

    def __init__(self, minions: Optional[list] = None, tier: int = 1, player_id: int = 0):
        self.minions: list[SimMinion] = minions or []
        self.hero_card_id = ""
        self.hero_power_card_id = ""
        self.tier = tier
        self.player_id = player_id
        self.attack_index = 0
        self.hero_health = 30
        self.hero_armor = 0
        self.trinkets: tuple[str, ...] = ()
        self.deaths_this_combat = 0
        # Everything that has died on this side, for "your first 2 Mechs that
        # died this combat" style deathrattles.
        self.graveyard: list[SimMinion] = []
        # Stats already include the game's own start-of-combat effects.
        self.post_start_of_combat = False
        # Minions sitting in hand. Known for our own board only — an opponent's
        # hand is not in the log, so their hand-summons stay unpredictable.
        self.hand: list[SimMinion] = []
        # What this side's hero power does to bodies that appear mid-fight.
        # Nothing on the board shows it, so it has to be carried here.
        self.summon_buff: Optional[dict] = None
        # The rest of the hero power's in-combat behaviour: a buff on whoever
        # scores a kill, a body that appears the moment a slot frees up.
        self.hero_power: Optional[dict] = None
        # Bodies that walk in the moment a slot frees up — a hero power's, a
        # trinket's. Each entry fires once. A board that starts the fight full
        # has not spent an unconditional one yet; anything smaller means the game
        # already summoned the body and it is standing in the snapshot.
        self.space_queue: list = []
        # Trinket Avenge counters: ``[threshold, spec, deaths_so_far]`` each.
        # Board-level, because a trinket is not a minion and dies with nothing.
        self.trinket_avenge: list = []
        # "Your Beetles have +5/+5 this game": a standing order on the tokens
        # this board summons from now on, keyed by (token card id, tribe) and
        # accumulating as the cards that grant it fire. Board-level because it
        # outlives whichever minion granted it — the point of the wording.
        self.token_buff: dict[tuple, list] = {}

    def clone(self) -> "SimBoard":
        b = SimBoard.__new__(SimBoard)
        b.minions = [m.clone() for m in self.minions]
        b.hero_card_id = self.hero_card_id
        b.hero_power_card_id = self.hero_power_card_id
        b.tier = self.tier
        b.player_id = self.player_id
        b.attack_index = self.attack_index
        b.hero_health = self.hero_health
        b.hero_armor = self.hero_armor
        b.trinkets = self.trinkets
        b.deaths_this_combat = self.deaths_this_combat
        b.graveyard = list(self.graveyard)
        b.post_start_of_combat = self.post_start_of_combat
        # Cards leave the hand as they are summoned, so each playthrough needs
        # its own copies rather than a shared list.
        b.hand = [m.clone() for m in self.hand]
        b.summon_buff = self.summon_buff
        b.hero_power = self.hero_power
        # Both carry per-playthrough state, so each run needs its own copies.
        b.space_queue = list(self.space_queue)
        b.trinket_avenge = [list(a) for a in self.trinket_avenge]
        b.token_buff = {k: list(v) for k, v in self.token_buff.items()}
        return b

    @property
    def alive(self) -> list[SimMinion]:
        return [m for m in self.minions if not m.dead]

    @property
    def is_empty(self) -> bool:
        return not any(not m.dead for m in self.minions)

    def taunts(self) -> list[SimMinion]:
        return [m for m in self.minions if not m.dead and m.taunt and not m.stealth]

    def targetable(self) -> list[SimMinion]:
        return [m for m in self.minions if not m.dead and not m.stealth]

    def index_of(self, minion: SimMinion) -> int:
        for i, m in enumerate(self.minions):
            if m is minion:
                return i
        return -1

    def insert(self, minion: SimMinion, index: int) -> bool:
        """Summon into a specific slot. Returns False when the board is full."""
        if len(self.alive) >= self.MAX_MINIONS:
            return False
        index = max(0, min(index, len(self.minions)))
        self.minions.insert(index, minion)
        if index <= self.attack_index:
            self.attack_index += 1
        return True

    def damage_score(self) -> int:
        """Damage this board deals to the losing hero: tavern tier plus the tier
        of every surviving minion."""
        return self.tier + sum(m.tier for m in self.minions if not m.dead)


def board_from_snapshot(snap: "BoardSnapshot",
                        hero_powers: Optional[dict] = None,
                        trinkets: Optional[dict] = None) -> SimBoard:
    """Convert the parsed log snapshot into simulator state.

    ``hero_powers`` maps a hero card id to the part of its power that keeps
    working during the fight (see ``CardDB.hero_power_specs``); ``trinkets`` does
    the same per trinket card id. Without them the boards simulate as if every
    hero and every accessory were a bystander.
    """
    board = SimBoard(tier=max(1, snap.tech_level), player_id=snap.player_id)
    power = (hero_powers or {}).get(snap.hero_card_id) or {}
    board.hero_power = power or None
    board.summon_buff = power.get("summon_buff")
    board.hero_card_id = snap.hero_card_id
    board.hero_power_card_id = snap.hero_power_card_id
    board.hero_health = snap.hero_health
    board.hero_armor = snap.hero_armor
    board.trinkets = tuple(snap.trinkets)
    board.post_start_of_combat = bool(getattr(snap, "post_start_of_combat", False))
    for i, s in enumerate(sorted(snap.minions, key=lambda m: m.position)):
        board.minions.append(minion_from_snapshot(s, uid=i + 1))
    full = len(board.minions) >= SimBoard.MAX_MINIONS
    when_space = power.get("when_space") or {}
    if when_space and full and snap.turn >= int(when_space.get("unlock_turn", 0)):
        board.space_queue.append({"copy": when_space.get("copy", "attack")})

    for card_id in board.trinkets:
        spec = _trinket_spec_for(card_id, trinkets)
        if not spec:
            continue
        space = spec.get("when_space")
        if space:
            # A body that needs something to die first cannot have been spent
            # before the fight; an unconditional one has, unless the board was
            # already full.
            conditional = bool((space.get("summon") or {}).get("from_graveyard"))
            if conditional or full:
                board.space_queue.append(space)
        avenge = spec.get("avenge")
        if avenge:
            board.trinket_avenge.append([int(avenge.get("threshold", 3)), avenge, 0])
    for i, s in enumerate(getattr(snap, "hand", ()) or ()):
        board.hand.append(minion_from_snapshot(s, uid=-(i + 1)))
    return board


def _trinket_spec_for(card_id: str, trinkets: Optional[dict]) -> dict:
    """Trinket specs, tolerating the upgraded ``...t`` card ids."""
    if not trinkets or not card_id:
        return {}
    spec = trinkets.get(card_id)
    if spec is None and card_id.endswith("t"):
        spec = trinkets.get(card_id[:-1])
    return spec or {}


def minion_from_snapshot(s: "MinionSnapshot", uid: int = 0) -> SimMinion:
    m = SimMinion(card_id=s.card_id, name=s.name, attack=s.attack,
                  health=max(1, s.health), tier=max(1, s.tier), golden=s.golden,
                  races=s.races)
    m.max_health = m.health
    m.taunt = s.taunt
    m.divine_shield = s.divine_shield
    m.poisonous = s.poisonous
    m.venomous = s.venomous
    m.reborn = s.reborn
    m.windfury = s.windfury
    m.mega_windfury = s.mega_windfury
    m.stealth = s.stealth
    m.cant_attack = s.cant_attack
    m.script_data = s.script_data
    m.uid = uid
    m.entity_id = s.entity_id
    return m
