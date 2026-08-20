"""Card behaviour for the combat engine.

Anything the log already tells us (stats, Divine Shield, Taunt, Poisonous,
Venomous, Reborn, Windfury) is handled by the engine itself. This module adds
the behaviour that is *not* in the log: deathrattles, Frenzy, Avenge, kill and
damage triggers, start-of-combat effects and cleave.

Rather than hard-coding hundreds of card ids that change every patch, effects
are **derived from the English card text** (see
:func:`hsbg.carddb.derive_effects`) into a plain, JSON-serialisable spec. That
spec is installed here with :func:`configure`, which also lets worker processes
receive it cheaply. :data:`MANUAL_EFFECTS` covers the handful of cards whose
text does not parse into anything useful.

The spec vocabulary the engine understands:

``damage``   list of actions ``{amount|source_attack, count, mode, side}``
``summon``   token stats, or ``pool`` filters for "summon a random Beast"
``buff``     ``{race, attack, health, keyword flags, count, adjacent, position}``
``grant``    list of ``{keywords, count, race, side, position}``
``transfer`` ``{stat, count}`` — hand this minion's Attack/Health to allies
``trigger_deathrattle``  ``{position, count}`` — re-fire an ally's deathrattle
``make_golden``          ``{position}`` — double a named slot's stats

Rally (printed as "after this attacks") additionally gets the minion that was
just hit, and carries payloads aimed at it: ``target_damage``,
``set_target_stats``, ``remove_target_keywords``, ``gain_target_attack``.
"""
from __future__ import annotations

import random
from typing import Any, Optional

from .model import SimBoard, SimMinion

# base card id (no ``_G`` suffix is stripped here — golden cards have their own
# entry so their doubled numbers come for free) -> spec dict
EFFECTS: dict[str, dict[str, Any]] = {}

# Minions that "summon a random X" pull from here; filled by carddb.derive_pool.
POOL: list[dict[str, Any]] = []
_POOL_CACHE: dict[tuple, list[dict[str, Any]]] = {}
_POOL_BY_ID: dict[str, dict[str, Any]] = {}
# card id -> resolved spec. spec_for runs ~150k times per prediction, so the
# golden-suffix fallback is resolved once per card instead of per lookup.
_SPEC_CACHE: dict[str, dict[str, Any]] = {}

# Cards whose text cannot be parsed usefully. Kept tiny on purpose.
MANUAL_EFFECTS: dict[str, dict[str, Any]] = {}

_unknown_deathrattles: set[str] = set()

KEYWORD_FLAGS = ("taunt", "divine_shield", "poisonous", "venomous", "reborn",
                 "windfury", "stealth")


def configure(spec: Optional[dict[str, dict[str, Any]]] = None,
              pool: Optional[list[dict[str, Any]]] = None) -> None:
    """Install a derived effect table (called in each worker process)."""
    EFFECTS.clear()
    if spec:
        EFFECTS.update(spec)
    EFFECTS.update(MANUAL_EFFECTS)
    POOL.clear()
    if pool:
        POOL.extend(pool)
    _POOL_CACHE.clear()
    _SPEC_CACHE.clear()
    _POOL_BY_ID.clear()
    _POOL_BY_ID.update({e["card_id"]: e for e in POOL if e.get("card_id")})


def spec_for(minion: SimMinion) -> dict[str, Any]:
    card_id = minion.card_id
    spec = _SPEC_CACHE.get(card_id)
    if spec is None:
        spec = EFFECTS.get(card_id) or EFFECTS.get(minion.base_card_id) or {}
        _SPEC_CACHE[card_id] = spec
    return spec


def has_effect(card_id: str) -> bool:
    return card_id in EFFECTS or (card_id.endswith("_G") and card_id[:-2] in EFFECTS)


def apply_static(minion: SimMinion) -> None:
    """Apply always-on properties that the log does not expose, such as cleave."""
    spec = spec_for(minion)
    if spec.get("cleave"):
        minion.cleave = True



def hero_on_kill(board: SimBoard, killer: SimMinion) -> None:
    """"After a friendly minion kills an enemy, give it +1 Attack" (Rokara).

    A hero power, so nothing on the board carries it and no snapshot can show
    it: by the time it matters the kill has not happened yet.
    """
    buff = (board.hero_power or {}).get("on_kill_buff")
    if buff and not killer.dead:
        _grow(killer, buff)


# --------------------------------------------------------------------------
# engine hooks
# --------------------------------------------------------------------------

def apply_static_all(combat) -> None:
    """Always-on properties (cleave), regardless of snapshot timing."""
    for board in combat.boards:
        for m in board.minions:
            apply_static(m)


def run_start_of_combat(combat) -> None:
    # Both sides' effects interleave; order is randomised because the real game
    # resolves them by tavern tier with ties broken arbitrarily.
    actors: list[tuple[SimBoard, SimMinion]] = []
    for board in combat.boards:
        for m in list(board.minions):
            soc = spec_for(m).get("start_of_combat")
            if soc and not soc.get("in_hand"):
                actors.append((board, m))
        # A few cards fire *from the hand* ("Start of Combat: If this minion is
        # in your hand, summon a copy of it") — they never stand on the board.
        for m in list(board.hand):
            soc = spec_for(m).get("start_of_combat")
            if soc and soc.get("in_hand"):
                actors.append((board, m))
    combat.rng.shuffle(actors)
    for board, minion in actors:
        if minion.dead:
            continue
        _start_of_combat(combat, board, minion)


def _start_of_combat(combat, board: SimBoard, minion: SimMinion) -> None:
    soc = spec_for(minion).get("start_of_combat") or {}

    multiplier = int(soc.get("self_multiplier", 0))
    if multiplier > 1:
        minion.attack *= multiplier
        minion.health *= multiplier
        minion.max_health = max(minion.max_health, minion.health)

    if soc.get("gain_hand_stats"):
        _grow(minion, {"attack": sum(m.attack for m in board.hand),
                       "health": sum(m.max_health for m in board.hand)})

    index = board.index_of(minion)
    if index < 0:                 # firing from hand: the body lands on the right
        index = len(board.minions)
    _run_actions(combat, board, minion, soc, index=index)


def on_before_attack(combat, board: SimBoard, attacker: SimMinion,
                     target: SimMinion) -> None:
    """Allies that react to one of their own swinging (Prodigious Tusker &co).

    Three payloads share this hook: ``gain_attacker`` grows the minion that just
    swung ("give it +3 Attack"), ``gain`` grows the watcher ("gain +1 Attack"),
    and a buff or summon fires from the watcher at the board. Cards that watch a
    single tribe — "whenever a friendly Beast attacks" — carry that tribe as a
    filter, and only the ones saying "another" skip their own swing.
    """
    for ally in board.minions:
        if ally.dead:
            continue
        spec = spec_for(ally).get("on_ally_attack")
        if not spec:
            continue
        if ally is attacker and spec.get("exclude_self", True):
            continue
        race = spec.get("race")
        if race and not attacker.is_race(race):
            continue
        if spec.get("gain_attacker"):
            _grow(attacker, spec["gain_attacker"])
        if spec.get("gain"):
            _grow(ally, spec["gain"])
        _run_actions(combat, board, ally, spec, index=board.index_of(ally))


def on_after_attack(combat, board: SimBoard, attacker: SimMinion, target: SimMinion) -> None:
    """"After this attacks" — which is what the Rally keyword prints as.

    Rally is the one trigger with a referent: "the target" is the minion that
    was just hit, and the engine hands it to us. Half the printed Rallies do
    something to it — strip its Taunt, shrink it to 3/3, steal its Attack — so
    without that referent they cannot be modelled at all.

    The allies that watch for somebody else's Rally go first: they react to the
    swing having happened, not to whatever the attacker's own Rally does next.
    """
    _on_rally_attack(combat, board, attacker)

    spec = spec_for(attacker).get("after_attack")
    if not spec or attacker.dead:
        return
    limit = int(spec.get("limit", 0))
    if limit and attacker.attacks_taken > limit:
        return

    multiplier = int(spec.get("self_multiplier", 0))
    if multiplier > 1:
        attacker.attack *= multiplier
        attacker.health *= multiplier
        attacker.max_health = max(attacker.max_health, attacker.health)
    if spec.get("gain_tier_attack"):
        _grow(attacker, {"attack": board.tier, "health": 0})
    if spec.get("gain"):
        _grow(attacker, spec["gain"])

    _apply_to_target(combat, board, attacker, target, spec)
    _run_actions(combat, board, attacker, spec, index=board.index_of(attacker))

    if spec.get("destroy_self"):
        attacker.health = min(attacker.health, 0)
        combat.resolve_deaths()


def _on_rally_attack(combat, board: SimBoard, attacker: SimMinion) -> None:
    """"After a friendly Rally minion attacks, ..." — Deathstrider.

    Two things separate it from the generic ally-attack watcher in
    :func:`on_before_attack`: it only counts allies that carry Rally, and it
    fires *after* the swing, because its payload re-triggers a deathrattle and
    the game resolves that on the board the swing left behind. A Windfury Rally
    minion therefore sets it off twice a turn, which is the entire point of the
    Beetle boards it shows up on.
    """
    if not spec_for(attacker).get("rally"):
        return
    for ally in list(board.minions):
        if ally.dead:
            continue
        spec = spec_for(ally).get("on_rally_attack")
        if not spec:
            continue
        if spec.get("gain"):
            _grow(ally, spec["gain"])
        _run_actions(combat, board, ally, spec, index=board.index_of(ally))


def _apply_to_target(combat, board: SimBoard, attacker: SimMinion,
                     target: SimMinion, spec: dict[str, Any]) -> None:
    """Rally payloads aimed at the minion that was just attacked."""
    defenders = combat.opponent_of(board)
    touched = False

    for keyword in spec.get("remove_target_keywords") or ():
        if getattr(target, keyword, False):
            setattr(target, keyword, False)

    stats = spec.get("set_target_stats")
    if stats and not target.dead:
        target.attack = int(stats[0])
        target.max_health = max(1, int(stats[1]))
        # Set, not healed: a 90/90 chopped to 3/3 is at 3 health, not 3 over 90.
        target.health = target.max_health
        touched = True

    if spec.get("gain_target_attack"):
        _grow(attacker, {"attack": target.attack, "health": 0})

    for action in spec.get("target_damage") or ():
        amount = attacker.attack if action.get("source_attack") else int(action.get("amount", 0))
        if amount <= 0:
            continue
        mode = action.get("mode", "target")
        if mode != "target_neighbours" and not target.dead:
            combat.deal_damage(target, amount, attacker, defenders)
            touched = True
        if mode in ("target_neighbours", "target_and_neighbour"):
            index = defenders.index_of(target)
            neighbours = [index - 1, index + 1]
            if mode == "target_and_neighbour":
                # "the target and an adjacent minion" — one neighbour, not both.
                neighbours = [n for n in neighbours
                              if 0 <= n < len(defenders.minions)
                              and not defenders.minions[n].dead]
                neighbours = ([neighbours[combat.rng.randrange(len(neighbours))]]
                              if neighbours else [])
            for n in neighbours:
                if 0 <= n < len(defenders.minions) and not defenders.minions[n].dead:
                    combat.deal_damage(defenders.minions[n], amount, attacker, defenders)
                    touched = True

    if touched:
        combat.resolve_deaths()


def on_damaged(combat, board: SimBoard, target: SimMinion,
               source: Optional[SimMinion], amount: int) -> None:
    """Frenzy and "whenever this takes damage" — both only fire on survival."""
    if target.health <= 0 or target.dead:
        return
    spec = spec_for(target)

    frenzy = spec.get("frenzy")
    if frenzy and target.damaged_count == 1:
        _react(combat, board, target, frenzy)

    damaged = spec.get("on_damaged")
    if damaged:
        limit = int(damaged.get("limit", 0))
        if not limit or target.damaged_count <= limit:
            _react(combat, board, target, damaged)


def on_kill(combat, board: SimBoard, killer: SimMinion, victim: SimMinion,
            attacking: bool = True) -> None:
    """"Whenever this kills a minion" plus the board-wide "a friendly minion
    kills an enemy" watchers."""
    if not killer.dead:
        spec = spec_for(killer).get("on_kill")
        if spec and not (spec.get("attacking_only") and not attacking):
            limit = int(spec.get("limit", 0))
            if not limit or killer.kill_count <= limit:
                if spec.get("gain_victim_stats"):
                    _grow(killer, {"attack": victim.attack, "health": victim.max_health})
                _react(combat, board, killer, spec)

    for ally in board.minions:
        if ally.dead or ally is killer:
            continue
        spec = spec_for(ally).get("on_friendly_kill")
        if spec:
            _react(combat, board, ally, spec)


def on_divine_shield_lost(combat, board: SimBoard, minion: SimMinion) -> None:
    for ally in board.minions:
        if ally.dead:
            continue
        spec = spec_for(ally).get("on_ally_shield_lost")
        if spec:
            _react(combat, board, ally, spec)


def deathrattle_repeats(board: SimBoard, dying: SimMinion) -> int:
    """How many times a deathrattle on this board fires — Titus Rivendare.

    An aura, not a payload: Titus carries nothing of his own, he multiplies
    everybody else's. Read as a vanilla body he turned a won fight into a lost
    prediction *at full reported coverage*. Counted at the moment the rattle
    goes off, so a Titus dying in the same sweep no longer doubles anything.
    """
    extra = 0
    for m in board.minions:
        if m.dead or m is dying:
            continue
        aura = spec_for(m).get("deathrattle_aura")
        if aura:
            extra += int(aura.get("extra", 1))
    return 1 + extra


def on_death(combat, board: SimBoard, minion: SimMinion, index: int) -> None:
    """The dying minion's own deathrattle."""
    spec = spec_for(minion)
    dr = spec.get("deathrattle")
    repeats = deathrattle_repeats(board, minion)
    if not dr:
        if spec.get("has_deathrattle"):
            _unknown_deathrattles.add(minion.card_id)
        for _ in range(repeats):
            for extra in minion.extra_deathrattles:
                _run_actions(combat, board, minion, extra, index=index)
        return

    for _ in range(repeats):
        _run_actions(combat, board, minion, dr, index=index)
        for extra in minion.extra_deathrattles:
            _run_actions(combat, board, minion, extra, index=index)

        killer = minion.killer
        if killer is not None and not killer.dead:
            if dr.get("destroy_killer"):
                killer.health = min(killer.health, 0)
                killer.killer = minion
            if dr.get("buff_killer"):
                _grow(killer, dr["buff_killer"])


def run_space_summon(combat, board: SimBoard, spec: dict[str, Any]) -> None:
    """A trinket's "when you have space, summon ..." payload.

    A trinket owns no body, so the payload has to fire from the board itself:
    the right-most slot, with a stand-in source for the few fields the summon
    vocabulary reads off one.
    """
    source = SimMinion(card_id="", name="trinket", attack=0, health=1,
                       tier=board.tier)
    _run_actions(combat, board, source, spec, index=len(board.minions))


def trinket_avenge(combat, board: SimBoard) -> None:
    """Board-level Avenge counters, which is the only shape a trinket has.

    Minion Avenge lives on the minion; a trinket has nowhere to keep a count, so
    the board does it — and unlike a minion's, it cannot be silenced or die.
    """
    for entry in board.trinket_avenge:
        threshold, spec, count = entry
        count += 1
        if count >= threshold:
            entry[2] = 0
            source = SimMinion(card_id="", name="trinket", attack=0, health=1,
                               tier=board.tier)
            _run_actions(combat, board, source, spec, index=len(board.minions))
        else:
            entry[2] = count


def on_any_death(combat, board: SimBoard, minion: SimMinion) -> None:
    """Allies reacting to a friendly death (Avenge, 'whenever a minion dies')."""
    trinket_avenge(combat, board)
    for ally in board.minions:
        if ally.dead or ally is minion:
            continue
        spec = spec_for(ally)

        trigger = spec.get("on_friendly_death")
        if trigger and (not trigger.get("race") or minion.is_race(trigger["race"])):
            limit = int(trigger.get("limit", 0))
            if not limit or board.deaths_this_combat <= limit:
                if trigger.get("gain_dead_attack"):
                    _grow(ally, {"attack": minion.attack, "health": 0})
                if trigger.get("gain_dead_stats"):
                    _grow(ally, {"attack": minion.attack, "health": minion.max_health})
                if trigger.get("gain_deathrattle"):
                    stolen = spec_for(minion).get("deathrattle")
                    if stolen:
                        ally.extra_deathrattles = ally.extra_deathrattles + (stolen,)
                    ally.extra_deathrattles += minion.extra_deathrattles
                _react(combat, board, ally, trigger)

        avenge = spec.get("avenge")
        if avenge:
            ally.avenge_counter += 1
            if ally.avenge_counter >= int(avenge.get("threshold", 3)):
                ally.avenge_counter = 0
                _react(combat, board, ally, avenge)


def on_summon(combat, board: SimBoard, minion: SimMinion) -> None:
    """A body appeared mid-combat: wake the allies that watch for it.

    Only summons *during* the fight reach here — deathrattles, Rally, Reborn —
    which is precisely what "Whenever you summon a Mech during combat" means.
    """
    apply_static(minion)
    for ally in board.minions:
        if ally.dead or ally is minion:
            continue
        spec = spec_for(ally).get("on_summon")
        if not spec:
            continue
        race = spec.get("race")
        if race and not minion.is_race(race):
            continue
        if spec.get("gain"):
            _grow(ally, spec["gain"])
        buff = spec.get("buff_summoned")
        if buff:
            _grow(minion, buff)
        for keyword in spec.get("gain_keywords") or ():
            setattr(ally, keyword, True)
        _run_actions(combat, board, ally, spec, index=board.index_of(ally))


# --------------------------------------------------------------------------
# action runner
# --------------------------------------------------------------------------

def _react(combat, board: SimBoard, minion: SimMinion, spec: dict[str, Any]) -> None:
    """Apply a trigger's payload to/around ``minion``."""
    if spec.get("gain"):
        _grow(minion, spec["gain"])
    _run_actions(combat, board, minion, spec, index=board.index_of(minion))


def _run_actions(combat, board: SimBoard, source: SimMinion,
                 spec: dict[str, Any], index: int) -> None:
    """The shared payload vocabulary: summon / buff / grant / damage."""
    # First, because the cards print it first: "Your Beetles have +5/+5 this
    # game. Summon a 2/2 Beetle." — the token that follows is meant to arrive
    # already carrying the raise it just granted.
    game_buff = spec.get("game_buff")
    if game_buff:
        add_token_buff(board, game_buff)

    summon = spec.get("summon")
    if summon:
        _do_summon(combat, board, source, summon, index)

    from_hand = spec.get("summon_from_hand")
    if from_hand:
        _summon_from_hand(combat, board, source, from_hand, index)

    buff = spec.get("buff")
    if buff:
        _buff_friendly(combat, board, source, buff,
                       exclude_self=bool(buff.get("exclude_self", True)))

    for grant in spec.get("grant") or ():
        _do_grant(combat, board, source, grant)

    transfer = spec.get("transfer")
    if transfer:
        _transfer_stats(combat, board, source, transfer)

    fire = spec.get("trigger_deathrattle")
    if fire:
        _trigger_deathrattle(combat, board, source, fire)

    golden = spec.get("make_golden")
    if golden:
        for ally in _at_position(board, source, golden.get("position", "rightmost")):
            ally.attack *= 2
            ally.max_health *= 2
            ally.health *= 2

    damage = spec.get("damage")
    if damage:
        for action in (damage if isinstance(damage, list) else [damage]):
            _do_damage(combat, board, source, action)
        combat.resolve_deaths()


def add_token_buff(board: SimBoard, spec: dict[str, Any]) -> None:
    """Record "your Beetles have +5/+5 this game" on the board.

    It is not a buff on anybody standing: it applies to the tokens summoned
    from here on, and it stacks every time a card that grants it fires. Keeping
    it on the board rather than on a minion is what makes it survive the death
    of whoever granted it.
    """
    key = (spec.get("card_id") or "", (spec.get("token") or "").rstrip("s"),
           spec.get("race") or "")
    slot = board.token_buff.get(key)
    if slot is None:
        board.token_buff[key] = [int(spec.get("attack", 0)), int(spec.get("health", 0))]
    else:
        slot[0] += int(spec.get("attack", 0))
        slot[1] += int(spec.get("health", 0))


def apply_token_buff(board: SimBoard, minion: SimMinion) -> None:
    """Hand a freshly summoned body whatever "this game" buffs name it."""
    if not board.token_buff:
        return
    name = (minion.name or "").strip().lower().rstrip("s")
    for (card_id, token, race), (attack, health) in board.token_buff.items():
        # Named token first, tribe second, bare name last: "your Beetles"
        # resolves to a card id, "your Beasts" only ever to a tribe, and a token
        # the card database does not know still has its printed name to go on.
        if card_id:
            if minion.base_card_id != card_id:
                continue
        elif race:
            if not minion.is_race(race):
                continue
        elif token and name != token:
            continue
        minion.attack += attack
        minion.health += health
        minion.max_health = max(minion.max_health, minion.health)


def _trigger_deathrattle(combat, board: SimBoard, source: SimMinion,
                         spec: dict[str, Any]) -> None:
    """"Trigger your left-most Deathrattle" — Monstrous Macaw and friends.

    Nobody dies: the other minion's deathrattle payload simply fires again from
    where that minion stands. Macaw re-firing a big deathrattle every swing is
    one of the strongest boards in the format, and read as a plain 4/3 it looked
    like a losing one.
    """
    position = spec.get("position", "leftmost")
    if position == "leftmost":
        pool = [m for m in board.minions
                if not m.dead and m is not source and spec_for(m).get("deathrattle")]
        picks = pool[:1]
    elif position == "rightmost":
        pool = [m for m in board.minions
                if not m.dead and m is not source and spec_for(m).get("deathrattle")]
        picks = pool[-1:]
    else:
        picks = [m for m in _at_position(board, source, position, combat.rng)
                 if spec_for(m).get("deathrattle")]
    if not picks:
        return
    for minion in picks:
        deathrattle = spec_for(minion).get("deathrattle") or {}
        # A re-fired deathrattle is still a deathrattle, so Titus multiplies it
        # exactly as he would the real one.
        times = max(1, int(spec.get("count", 1))) * deathrattle_repeats(board, minion)
        for _ in range(times):
            _run_actions(combat, board, minion, deathrattle,
                         index=board.index_of(minion))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _pool_for(filters: dict[str, Any]) -> list[dict[str, Any]]:
    key = (filters.get("race"), filters.get("mechanic"), bool(filters.get("legendary")),
           tuple(filters.get("tiers") or ()), int(filters.get("max_tier", 0)))
    cached = _POOL_CACHE.get(key)
    if cached is not None:
        return cached
    race, mechanic, legendary, tiers, max_tier = key
    out = []
    for entry in POOL:
        if race and race not in entry.get("races", ()):
            continue
        if mechanic and mechanic not in entry.get("mechanics", ()):
            continue
        if legendary and not entry.get("legendary"):
            continue
        if tiers and entry.get("tier") not in tiers:
            continue
        if max_tier and entry.get("tier", 1) > max_tier:
            continue
        out.append(entry)
    _POOL_CACHE[key] = out
    return out


def _token_from_entry(entry: dict[str, Any], source: SimMinion) -> SimMinion:
    token = SimMinion(
        card_id=entry.get("card_id", ""),
        name=entry.get("name", "token"),
        attack=int(entry.get("attack", 1)),
        health=max(1, int(entry.get("health", 1))),
        tier=int(entry.get("tier", source.tier)),
        races=frozenset(entry.get("races", ()) or ()),
    )
    token.max_health = token.health
    for flag in KEYWORD_FLAGS:
        if entry.get(flag):
            setattr(token, flag, True)
    return token


def _summon_from_hand(combat, board: SimBoard, source: SimMinion,
                      spec: dict[str, Any], index: int) -> None:
    """"Summon the highest-Attack minion from your hand for this combat."

    Silently does nothing when the hand is unknown, which is always the case for
    the opponent — the log never shows it. That is the honest answer: guessing a
    body would be worse than leaving it out.
    """
    if not board.hand:
        return
    race = spec.get("race")
    by = spec.get("by", "attack")
    buff = spec.get("buff")
    for _ in range(int(spec.get("count", 1))):
        pool = [m for m in board.hand if not race or m.is_race(race)]
        if not pool:
            return
        if by == "health":
            pick = max(pool, key=lambda m: (m.max_health, m.attack))
        elif by == "random":
            pick = pool[combat.rng.randrange(len(pool))]
        else:
            pick = max(pool, key=lambda m: (m.attack, m.max_health))
        board.hand.remove(pick)
        body = pick.clone()
        body.uid = 0
        if buff:
            _grow(body, buff)
        if not combat.summon(board, body, index):
            return


def _do_summon(combat, board: SimBoard, source: SimMinion,
               summon: dict[str, Any], index: int) -> None:
    count = int(summon.get("count", 1))
    if summon.get("count_from_attack"):
        count = max(0, source.attack)
    count = min(count, SimBoard.MAX_MINIONS)

    pool_filters = summon.get("pool")
    use_source = bool(summon.get("use_source_stats"))
    set_stats = summon.get("set_stats")

    graveyard = summon.get("from_graveyard")
    if graveyard is not None:
        _summon_from_graveyard(combat, board, source, graveyard, count, index)
        return

    if summon.get("copy_self"):
        for _ in range(count):
            copy = source.clone()
            copy.dead = False
            copy.killer = None
            copy.attacks_taken = 0
            copy.damaged_count = 0
            copy.kill_count = 0
            if summon.get("double_stats"):
                copy.attack *= 2
                copy.health *= 2
                copy.max_health = copy.health
            if not combat.summon(board, copy, index):
                return
        return

    for _ in range(count):
        if pool_filters:
            candidates = _pool_for(pool_filters)
            if not candidates:
                return
            entry = candidates[combat.rng.randrange(len(candidates))]
            token = _token_from_entry(entry, source)
            if summon.get("golden"):
                token.attack *= 2
                token.health *= 2
                token.max_health = token.health
        else:
            attack = source.attack if use_source else int(summon.get("attack", 1))
            health = source.max_health if use_source else int(summon.get("health", 1))
            token = SimMinion(
                card_id=summon.get("card_id", f"token:{source.card_id}"),
                name=summon.get("name", "token"),
                attack=attack,
                health=max(1, health),
                tier=int(summon.get("tier", source.tier)),
                races=(source.races if use_source or summon.get("inherit_races")
                       else frozenset(summon.get("races") or ())),
            )
            for flag in KEYWORD_FLAGS:
                if summon.get(flag):
                    setattr(token, flag, True)
        if set_stats:
            token.attack, token.health = int(set_stats[0]), max(1, int(set_stats[1]))
            token.max_health = token.health
        if not combat.summon(board, token, index):
            return


def _base_stats(card_id: str) -> Optional[dict[str, Any]]:
    return _POOL_BY_ID.get(card_id) or _POOL_BY_ID.get(card_id[:-2] if
                                                       card_id.endswith("_G") else card_id)


def _summon_from_graveyard(combat, board: SimBoard, source: SimMinion,
                           filters: dict[str, Any], count: int, index: int) -> None:
    """Kangor's Apprentice: plain copies of the first N Mechs that died."""
    race = filters.get("race")
    picked = 0
    for dead in board.graveyard:
        if picked >= count:
            break
        if dead is source or (race and not dead.is_race(race)):
            continue
        base = None if filters.get("exact") else _base_stats(dead.card_id)
        token = _token_from_entry(base, source) if base else dead.clone()
        if base is None:
            # No card entry to fall back on: revive it as it stood, minus state.
            token.dead = False
            token.entity_id = 0
            token.killer = None
            token.attacks_taken = 0
            token.damaged_count = 0
            token.kill_count = 0
            token.health = token.max_health
        picked += 1
        if not combat.summon(board, token, index):
            return


def _do_grant(combat, board: SimBoard, source: SimMinion, grant: dict[str, Any]) -> None:
    """"Give N random friendly minions Divine Shield" and friends."""
    keywords = grant.get("keywords") or ()
    if not keywords:
        return
    position = grant.get("position")
    if position:
        for ally in _at_position(board, source, position):
            for keyword in keywords:
                setattr(ally, keyword, True)
        return
    side = grant.get("side", "friendly")
    target_board = board if side == "friendly" else combat.opponent_of(board)
    race = grant.get("race")
    exclude_self = bool(grant.get("exclude_self", True)) and side == "friendly"

    pool = [m for m in target_board.minions
            if not m.dead
            and not (exclude_self and m is source)
            and (not race or m.is_race(race))]
    # Prefer minions that do not already have the keyword: the real game only
    # wastes a Divine Shield grant when nothing else is eligible.
    fresh = [m for m in pool if not all(getattr(m, k, False) for k in keywords)]
    pool = fresh or pool
    if not pool:
        return

    count = min(int(grant.get("count", 1)), len(pool))
    for minion in _sample(combat.rng, pool, count):
        for keyword in keywords:
            setattr(minion, keyword, True)


def _at_position(board: SimBoard, source: SimMinion, position: str,
                 rng: Optional[random.Random] = None) -> list[SimMinion]:
    """Living allies a payload names by slot rather than by tribe.

    "the minion to the right of this", "your left-most minion", "an adjacent
    minion" — position is the whole point of these cards, so resolving it
    against the live board (skipping the caster) is what makes them work.
    """
    living = [m for m in board.minions if not m.dead]
    if not living:
        return []
    others = [m for m in living if m is not source]
    if position == "leftmost":
        return others[:1] if others else []
    if position == "rightmost":
        return others[-1:] if others else []

    index = board.index_of(source)
    if index < 0:
        return []
    if position == "right":
        picks = [m for m in board.minions[index + 1:] if not m.dead][:1]
        return picks
    if position == "left":
        picks = [m for m in board.minions[:index] if not m.dead][-1:]
        return picks
    # "adjacent": both neighbours, or one of them when the payload wants one.
    picks = [m for m in board.minions[:index] if not m.dead][-1:]
    picks += [m for m in board.minions[index + 1:] if not m.dead][:1]
    if rng is not None and len(picks) > 1:
        return [picks[rng.randrange(len(picks))]]
    return picks


def _sample(rng: random.Random, pool: list, count: int) -> list:
    if count >= len(pool):
        return list(pool)
    return rng.sample(pool, count)


def _transfer_stats(combat, board: SimBoard, source: SimMinion,
                    transfer: dict[str, Any]) -> None:
    """"Give this minion's Attack to a random friendly minion" (Fiendish Servant)."""
    race = transfer.get("race")
    pool = [m for m in board.minions
            if not m.dead and m is not source and (not race or m.is_race(race))]
    if not pool:
        return
    stat = transfer.get("stat", "attack")
    attack = source.attack if stat in ("attack", "both") else 0
    health = source.max_health if stat in ("health", "both") else 0
    if transfer.get("attack_as_health"):
        attack, health = 0, source.attack
    count = min(int(transfer.get("count", 1)), len(pool))
    for minion in _sample(combat.rng, pool, count):
        _grow(minion, {"attack": attack, "health": health})


def _damage_targets(combat, board: SimBoard, source: SimMinion,
                    action: dict[str, Any]) -> tuple[SimBoard, list[SimMinion]]:
    side = action.get("side", "enemy")
    enemies = combat.opponent_of(board)
    target_board = board if side == "friendly" else enemies
    pool = [m for m in target_board.minions if not m.dead and not m.stealth]
    except_race = action.get("except_race")
    if except_race and side != "enemy":
        pool = [m for m in pool if not m.is_race(except_race)]
    if side == "friendly" and action.get("exclude_self"):
        pool = [m for m in pool if m is not source]
    return target_board, pool


def _do_damage(combat, board: SimBoard, source: SimMinion, action: dict[str, Any]) -> None:
    amount = int(action.get("amount", 0))
    if action.get("source_attack"):
        amount = source.attack
    if amount <= 0:
        return
    mode = action.get("mode", "random")
    side = action.get("side", "enemy")

    if side == "all":
        for which in ("enemy", "friendly"):
            _do_damage(combat, board, source, {**action, "side": which, "mode": mode})
        return

    target_board, pool = _damage_targets(combat, board, source, action)
    if not pool:
        return

    if mode == "all":
        for victim in list(pool):
            combat.deal_damage(victim, amount, source, target_board)
        return

    if mode == "nearest":
        # Positional: the enemies facing this minion's own slot outwards.
        index = min(board.index_of(source), len(pool) - 1)
        order = sorted(range(len(pool)), key=lambda i: (abs(i - index), i))
        for i in order[:int(action.get("count", 1))]:
            combat.deal_damage(pool[i], amount, source, target_board)
        return

    if mode == "adjacent":
        index = target_board.index_of(source)
        for neighbour in (index - 1, index + 1):
            if 0 <= neighbour < len(target_board.minions):
                victim = target_board.minions[neighbour]
                if not victim.dead:
                    combat.deal_damage(victim, amount, source, target_board)
        return

    for _ in range(int(action.get("count", 1))):
        live = [m for m in pool if not m.dead]
        if not live:
            return
        if mode == "highest_health":
            victim = max(live, key=lambda m: m.health)
        elif mode == "lowest_health":
            victim = min(live, key=lambda m: m.health)
        else:
            victim = live[combat.rng.randrange(len(live))]
        combat.deal_damage(victim, amount, source, target_board)


def _grow(minion: SimMinion, gain: dict[str, Any]) -> None:
    minion.attack = max(0, minion.attack + int(gain.get("attack", 0)))
    health = int(gain.get("health", 0))
    minion.health += health
    minion.max_health += health
    for flag in KEYWORD_FLAGS:
        if gain.get(flag):
            setattr(minion, flag, True)


def _buff_friendly(combat, board: SimBoard, source: SimMinion,
                   buff: dict[str, Any], exclude_self: bool) -> None:
    race = buff.get("race")
    parity = buff.get("tier_parity")

    position = buff.get("position")
    if position:
        for ally in _at_position(board, source, position):
            _grow(ally, buff)
        return

    if buff.get("adjacent"):
        index = board.index_of(source)
        for neighbour in (index - 1, index + 1):
            if 0 <= neighbour < len(board.minions) and not board.minions[neighbour].dead:
                _grow(board.minions[neighbour], buff)
        return

    pool = []
    for ally in board.minions:
        if ally.dead or (exclude_self and ally is source):
            continue
        if race and not ally.is_race(race):
            continue
        if parity and (ally.tier % 2 == 0) != (parity == "even"):
            continue
        pool.append(ally)
    if not pool:
        return

    if buff.get("per_type"):
        pool = _one_per_tribe(combat.rng, pool)
    count = int(buff.get("count", 0))
    if count:
        pool = _sample(combat.rng, pool, min(count, len(pool)))
    for ally in pool:
        _grow(ally, buff)


def _one_per_tribe(rng: random.Random, pool: list[SimMinion]) -> list[SimMinion]:
    """"Give a friendly minion of each type ...": one random target per tribe."""
    by_race: dict[str, list[SimMinion]] = {}
    for ally in pool:
        for race in (ally.races or ("",)):
            by_race.setdefault(race, []).append(ally)
    chosen: list[SimMinion] = []
    for race in sorted(by_race):
        pick = by_race[race][rng.randrange(len(by_race[race]))]
        if pick not in chosen:
            chosen.append(pick)
    return chosen


def unknown_deathrattles() -> set[str]:
    """Cards seen dying with an unmodelled deathrattle — feeds the UI's
    confidence indicator."""
    return set(_unknown_deathrattles)


def reset_unknowns() -> None:
    _unknown_deathrattles.clear()


configure({})
