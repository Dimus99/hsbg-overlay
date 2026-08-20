"""Sanity checks for the combat engine and the log parser.

Run with:  ./.venv/bin/python -m pytest tests -q
        or ./.venv/bin/python tests/test_sim.py
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hsbg.gamestate import BattlegroundsState                 # noqa: E402
from hsbg.sim import effects                                   # noqa: E402
from hsbg.sim.engine import Combat                             # noqa: E402
from hsbg.sim.model import SimBoard, SimMinion                 # noqa: E402
from hsbg.carddb import parse_card_effects                     # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def minion(attack: int, health: int, **kwargs) -> SimMinion:
    m = SimMinion(card_id=kwargs.pop("card_id", "T"), name="m",
                  attack=attack, health=health, tier=kwargs.pop("tier", 1))
    for key, value in kwargs.items():
        setattr(m, key, value)
    return m


def board(*minions: SimMinion, tier: int = 1) -> SimBoard:
    b = SimBoard(tier=tier)
    b.minions.extend(minions)
    return b


def play(a: SimBoard, b: SimBoard, seed: int = 1):
    return Combat(a.clone(), b.clone(), random.Random(seed)).run()


def outcomes(a: SimBoard, b: SimBoard, n: int = 400) -> dict[str, int]:
    counts = {"win": 0, "loss": 0, "tie": 0}
    rng = random.Random(7)
    for _ in range(n):
        counts[Combat(a.clone(), b.clone(), rng).run().result] += 1
    return counts


def test_combat() -> None:
    print("combat engine:")
    effects.configure({})

    check("equal 1/1 boards trade into a tie",
          play(board(minion(1, 1)), board(minion(1, 1))).result == "tie")

    check("empty vs a minion loses",
          play(board(), board(minion(1, 1))).result == "loss")

    check("empty vs empty is a tie",
          play(board(), board()).result == "tie")

    r = play(board(minion(5, 5)), board(minion(1, 1)))
    check("5/5 beats 1/1", r.result == "win")
    check("damage = winner tier + surviving minion tiers", r.damage == 2, f"got {r.damage}")

    # Divine Shield absorbs the first hit entirely.
    counts = outcomes(board(minion(1, 1, divine_shield=True)), board(minion(1, 1)))
    check("divine shield survives an equal trade", counts["win"] == 400, str(counts))

    # Poison kills through any amount of health.
    counts = outcomes(board(minion(1, 1, poisonous=True)), board(minion(10, 10)))
    check("poison kills a 10/10", counts["win"] + counts["tie"] == 400, str(counts))

    # Venomous is consumed after one kill.
    counts = outcomes(board(minion(1, 5, venomous=True)), board(minion(1, 6), minion(1, 6)))
    check("venomous only kills once", counts["loss"] == 400, str(counts))

    # Taunt forces the attack even when a juicier target exists.
    b = board(minion(1, 1, taunt=True), minion(0, 20))
    counts = outcomes(board(minion(1, 1)), b)
    check("taunt soaks the attack", counts["loss"] == 400, str(counts))

    # Windfury attacks twice per turn.
    counts = outcomes(board(minion(2, 10, windfury=True)), board(minion(1, 4)))
    check("windfury doubles up", counts["win"] == 400, str(counts))

    # Reborn brings the minion back with 1 health.
    counts = outcomes(board(minion(1, 1, reborn=True)), board(minion(1, 1)))
    check("reborn survives the first death", counts["win"] == 400, str(counts))

    # A board of only stealthed minions cannot be attacked — must not hang.
    r = play(board(minion(1, 1)), board(minion(1, 1, stealth=True)))
    check("all-stealth defender terminates", r.result in ("win", "loss", "tie"))

    # Deathrattle summon from the effect registry.
    effects.configure({"DR": {"has_deathrattle": True, "deathrattle": {
        "summon": {"count": 2, "attack": 3, "health": 3, "name": "tok"}}}})
    counts = outcomes(board(minion(1, 1, card_id="DR")), board(minion(1, 1)))
    check("deathrattle summons tokens that keep fighting",
          counts["win"] == 400, str(counts))
    effects.configure({})


def test_triggers() -> None:
    """The behaviour that is not in the log: deathrattle payloads, Frenzy,
    kill triggers and random-pool summons."""
    print("card triggers:")

    # Deathrattle damage wipes the enemy board on the way out.
    effects.configure({"BOMB": {"has_deathrattle": True, "deathrattle": {
        "damage": [{"amount": 5, "mode": "all", "side": "enemy"}]}}})
    counts = outcomes(board(minion(1, 1, card_id="BOMB")),
                      board(minion(1, 4), minion(1, 4)))
    check("deathrattle damage hits every enemy", counts["tie"] == 400, str(counts))

    # Frenzy fires once, on the first hit survived.
    effects.configure({"FRZ": {"frenzy": {"gain": {"attack": 0, "health": 10}}}})
    counts = outcomes(board(minion(2, 3, card_id="FRZ")), board(minion(2, 6)))
    check("frenzy triggers on surviving damage", counts["win"] == 400, str(counts))

    # "Give a random friendly minion Divine Shield" on death: the same board has
    # to do strictly better with the deathrattle than without it.
    mine = board(minion(1, 1, card_id="SELF"), minion(2, 2))
    theirs = board(minion(2, 2), minion(1, 1))
    effects.configure({})
    plain = outcomes(mine, theirs)["win"]
    effects.configure({"SELF": {"has_deathrattle": True, "deathrattle": {
        "grant": [{"keywords": ["divine_shield"], "count": 1, "side": "friendly"}]}}})
    granted = outcomes(mine, theirs)["win"]
    check("deathrattle grants a keyword to an ally", granted > plain,
          f"{granted} vs {plain} wins")

    # Fiendish Servant hands its Attack to a survivor.
    effects.configure({"FIEND": {"has_deathrattle": True, "deathrattle": {
        "transfer": {"stat": "attack", "count": 1}}}})
    counts = outcomes(board(minion(5, 1, card_id="FIEND"), minion(1, 6)),
                      board(minion(1, 5), minion(1, 5)))
    check("deathrattle transfers attack to an ally", counts["win"] == 400, str(counts))

    # Leeroy takes his killer with him.
    effects.configure({"LEE": {"has_deathrattle": True,
                               "deathrattle": {"destroy_killer": True}}})
    counts = outcomes(board(minion(1, 1, card_id="LEE")), board(minion(1, 20)))
    check("deathrattle destroys the killer", counts["tie"] == 400, str(counts))

    # The Boogeymonster grows on every kill.
    effects.configure({"BOOG": {"on_kill": {"gain": {"attack": 2, "health": 2}}}})
    counts = outcomes(board(minion(2, 5, card_id="BOOG")),
                      board(minion(1, 2), minion(3, 5)))
    check("kill trigger grows the attacker", counts["win"] == 400, str(counts))

    # "Summon a random Beast" pulls from the token pool.
    effects.configure({"COIL": {"has_deathrattle": True, "deathrattle": {
        "summon": {"count": 2, "pool": {"race": "BEAST", "max_tier": 6}}}}},
        pool=[{"card_id": "TOK", "name": "Big Beast", "attack": 6, "health": 6,
               "tier": 3, "races": ("BEAST",), "mechanics": ()}])
    counts = outcomes(board(minion(1, 1, card_id="COIL")), board(minion(4, 4)))
    check("random-pool deathrattle summons real minions", counts["win"] == 400, str(counts))

    # Kangor's Apprentice rebuilds from the graveyard, at printed stats.
    effects.configure({"KANG": {"has_deathrattle": True, "deathrattle": {
        "summon": {"count": 1, "from_graveyard": {"race": "MECHANICAL"}}}}},
        pool=[{"card_id": "MECH", "name": "Mech", "attack": 4, "health": 4,
               "tier": 2, "races": ("MECHANICAL",), "mechanics": ()}])
    mech = minion(9, 1, card_id="MECH")
    mech.races = frozenset({"MECHANICAL"})
    mine = board(mech, minion(1, 1, card_id="KANG"))
    theirs = board(minion(1, 9), minion(2, 2))
    revived = outcomes(mine, theirs)["win"]
    effects.configure({})
    plain = outcomes(mine, theirs)["win"]
    check("graveyard summon returns a plain copy", revived > plain,
          f"{revived} vs {plain} wins")

    effects.configure({})


def test_rally_and_hand() -> None:
    """Rally (the keyword the game prints for "after this attacks") and the
    cards that pull a body out of your hand mid-fight."""
    print("rally and hand summons:")

    # Rally: Gain +2 Attack — a 2/20 that grows every swing gets through a 4/10
    # it could never chew through at its printed Attack.
    effects.configure({})
    plain = outcomes(board(minion(2, 20, card_id="RALLY")), board(minion(4, 10)))["win"]
    effects.configure({"RALLY": {"after_attack": {"gain": {"attack": 2, "health": 0}}}})
    with_rally = outcomes(board(minion(2, 20, card_id="RALLY")), board(minion(4, 10)))["win"]
    check("rally attack growth wins a fight it otherwise loses",
          plain == 0 and with_rally == 400, f"{plain} -> {with_rally}")

    # "Rally: Summon the highest-Attack minion from your hand."
    effects.configure({"AVI": {"after_attack": {
        "summon_from_hand": {"count": 1, "race": None}}}})
    mine = board(minion(1, 4, card_id="AVI"))
    mine.hand = [minion(2, 2), minion(9, 9)]
    check("summons the biggest body in hand, once per swing",
          play(mine, board(minion(1, 8))).result == "win")

    # No hand known (every opponent board): the trigger must stay quiet.
    check("no hand means no phantom body",
          play(board(minion(1, 4, card_id="AVI")), board(minion(1, 8))).result == "loss")

    # Start of Combat firing from hand, for a card that never stands on board.
    effects.configure({"SCOUT": {"start_of_combat": {
        "in_hand": True, "summon": {"copy_self": True, "count": 1}}}})
    mine = board(minion(1, 1))
    mine.hand = [minion(6, 6, card_id="SCOUT")]
    check("a hand-only start-of-combat body joins the fight",
          play(mine, board(minion(2, 2))).result == "win")

    # "Whenever you summon a Beast, give it +2/+2" — the buff lands on the new
    # body, not on the whole board.
    effects.configure({
        "WATCH": {"on_summon": {"race": "BEAST",
                                "buff_summoned": {"attack": 5, "health": 5}}},
        "MAKER": {"has_deathrattle": True,
                  "deathrattle": {"summon": {"count": 1, "attack": 1, "health": 1,
                                             "races": ["BEAST"]}}},
    })
    watcher = minion(1, 10, card_id="WATCH")
    maker = SimMinion(card_id="MAKER", name="m", attack=1, health=1, tier=1,
                      races=frozenset({"BEAST"}))
    result = play(board(watcher, maker), board(minion(2, 2), minion(2, 2)))
    check("summon watcher fires on a deathrattle body", result.result in ("win", "tie"),
          result.result)


def test_scaling_packages() -> None:
    """The three board-wide mechanics that turn a losing-looking warband into a
    winning one, each of which used to simulate as a vanilla body."""
    print("scaling packages:")

    # Titus Rivendare: an aura with no payload of its own. The same deathrattle
    # has to put twice as many bodies on the board beside him.
    dr = {"has_deathrattle": True,
          "deathrattle": {"summon": {"count": 1, "attack": 4, "health": 4}}}
    effects.configure({"MAKER": dr, "TITUS": {"deathrattle_aura": {"extra": 1}}})
    mine = board(minion(1, 1, card_id="MAKER"), minion(1, 20, card_id="TITUS"))
    theirs = board(minion(1, 1), minion(4, 4), minion(4, 4))
    doubled = outcomes(mine, theirs)["win"]
    effects.configure({"MAKER": dr})
    plain = outcomes(board(minion(1, 1, card_id="MAKER"), minion(1, 20)), theirs)["win"]
    check("Titus doubles a deathrattle", doubled > plain, f"{plain} -> {doubled}")

    # ...and stops mattering the moment he is off the board: an aura is not a
    # deathrattle, so a Titus dying in the same sweep must not double anything.
    effects.configure({"MAKER": dr, "TITUS": {"deathrattle_aura": {"extra": 1}}})
    b = board(minion(1, 1, card_id="MAKER"), minion(1, 1, card_id="TITUS"))
    dead_titus = outcomes(b, theirs)["win"]
    check("a dead Titus doubles nothing", dead_titus <= doubled,
          f"{dead_titus} vs {doubled}")

    # Deathstrider: fires somebody else's deathrattle after an ally with Rally
    # swings. The watcher is not the attacker, and a minion without Rally must
    # leave it alone.
    big = {"has_deathrattle": True,
           "deathrattle": {"summon": {"count": 1, "attack": 8, "health": 8}}}
    strider = {"on_rally_attack": {"trigger_deathrattle": {"position": "leftmost",
                                                          "count": 1}}}
    mine = board(minion(1, 30, card_id="MAKER"), minion(2, 30, card_id="SWINGER"),
                 minion(1, 30, card_id="STRIDER"))
    theirs = board(minion(6, 60))
    effects.configure({"MAKER": big, "STRIDER": strider, "SWINGER": {"rally": True}})
    with_rally = outcomes(mine, theirs)["win"]
    # The same board with the Rally keyword taken off the swinger: the watcher
    # is still there, and must now do nothing at all.
    effects.configure({"MAKER": big, "STRIDER": strider, "SWINGER": {}})
    without = outcomes(mine, theirs)["win"]
    check("a Rally swing re-fires the left-most deathrattle",
          without == 0 and with_rally > 200, f"{without} -> {with_rally}")

    # "Your Beetles have +5/+5 this game": not a buff on anyone standing, but a
    # standing order on the tokens summoned afterwards — and it stacks.
    token = {"count": 1, "attack": 2, "health": 2, "card_id": "BEETLE",
             "name": "Beetle"}
    effects.configure({"SKIT": {"has_deathrattle": True, "deathrattle": {
        "summon": token,
        "game_buff": {"card_id": "BEETLE", "token": "beetle", "attack": 5,
                      "health": 5}}}})
    out = play(board(minion(1, 1, card_id="SKIT"), minion(1, 1, card_id="SKIT")),
               board(minion(1, 1), minion(1, 1), minion(9, 9)))
    check("beetles arrive carrying the buff their makers left", out.result == "win",
          out.result)

    # The order outlives whoever granted it, and only names its own token.
    effects.configure({"SKIT": {"has_deathrattle": True, "deathrattle": {
        "summon": {"count": 1, "attack": 2, "health": 2, "card_id": "OTHER",
                   "name": "Other"},
        "game_buff": {"card_id": "BEETLE", "token": "beetle", "attack": 5,
                      "health": 5}}}})
    out = play(board(minion(1, 1, card_id="SKIT"), minion(1, 1, card_id="SKIT")),
               board(minion(1, 1), minion(1, 1), minion(9, 9)))
    check("a buff aimed at Beetles skips a body that is not one",
          out.result != "win", out.result)

    effects.configure({})


def test_keywords() -> None:
    """The printed keyword list, each one exercised end to end.

    These are the mechanics that decide most fights, so each gets a board where
    turning it off flips the result — a check that passes with the keyword
    ignored is not checking the keyword.
    """
    print("printed keywords:")
    effects.configure({})

    check("divine shield eats one hit",
          play(board(minion(1, 1, divine_shield=True)), board(minion(5, 1))).result == "win")
    check("poison kills through any health",
          play(board(minion(1, 10, poisonous=True)), board(minion(1, 50))).result == "win")
    # Venomous is spent on the first kill; Poisonous keeps killing. Same board,
    # and the keyword is the only thing that differs.
    check("venom kills once and is spent",
          play(board(minion(1, 3, venomous=True)),
               board(minion(1, 50), minion(1, 50))).result == "loss")
    check("poison keeps killing",
          play(board(minion(1, 3, poisonous=True)),
               board(minion(1, 50), minion(1, 50))).result == "win")
    check("divine shield beats poison",
          play(board(minion(2, 2, divine_shield=True)),
               board(minion(1, 1, poisonous=True))).result == "win")
    check("taunt is attacked first",
          play(board(minion(2, 2), minion(9, 9)),
               board(minion(3, 3, taunt=True), minion(1, 1))).result in ("win", "tie"))
    check("windfury swings twice",
          play(board(minion(2, 5, windfury=True)), board(minion(2, 4))).result == "win")
    check("mega-windfury swings four times",
          play(board(minion(1, 9, mega_windfury=True)), board(minion(1, 4))).result == "win")
    check("reborn comes back with one health",
          play(board(minion(2, 2, reborn=True)), board(minion(2, 2))).result == "win")
    check("reborn does not come back twice",
          play(board(minion(2, 2, reborn=True)),
               board(minion(2, 2), minion(2, 2), minion(2, 2))).result == "loss")
    check("stealth cannot be attacked, and a stand-off is a draw",
          play(board(minion(0, 1, stealth=True)), board(minion(5, 5))).result == "tie")

    # Hero powers that keep working inside the fight. Nothing on either board
    # shows them, so a snapshot alone can never tell these boards apart.
    effects.configure({"DR": {"has_deathrattle": True,
                              "deathrattle": {"summon": {"count": 1, "attack": 1,
                                                         "health": 1}}}})
    plain = board(minion(1, 1, card_id="DR"))
    buffed = board(minion(1, 1, card_id="DR"))
    buffed.summon_buff = {"attack": 4, "health": 4}
    buffed.hero_power = {"summon_buff": buffed.summon_buff}
    check("greybough's buff lands on bodies summoned mid-fight",
          play(plain, board(minion(1, 1), minion(3, 3))).result == "loss"
          and play(buffed, board(minion(1, 1), minion(3, 3))).result == "win")

    rokara = board(minion(1, 12))
    rokara.hero_power = {"on_kill_buff": {"attack": 5, "health": 0}}
    enemy = board(minion(1, 1), minion(1, 20))
    plain_wins = outcomes(board(minion(1, 12)), enemy)["win"]
    rokara_wins = outcomes(rokara, enemy)["win"]
    check("rokara grows whoever scores a kill",
          plain_wins == 0 and rokara_wins > 300, f"{plain_wins} -> {rokara_wins}")

    full = board(*[minion(1, 1) for _ in range(6)], minion(9, 9))
    enemy = board(minion(2, 2), minion(2, 2), minion(2, 2))
    without = play(full, enemy)
    full.space_queue = [{"copy": "attack"}]
    with_power = play(full, enemy)
    check("drek'thar fills the first slot that opens",
          with_power.damage >= without.damage, f"{without.damage} -> {with_power.damage}")


def test_trinkets() -> None:
    """Accessories: the half of them that still acts inside the fight."""
    print("trinkets:")
    from hsbg.carddb import get_db

    effects.configure({})
    # "When you have space, summon an Ancestral Automaton" — the body only
    # appears once a slot frees up, and until it did the simulator was a whole
    # minion short for the rest of the fight.
    mine = board(*[minion(1, 1) for _ in range(7)])
    enemy = board(*[minion(1, 1) for _ in range(7)])
    without = outcomes(mine, enemy)
    mine.space_queue = [{"summon": {"count": 1, "attack": 6, "health": 6,
                                    "name": "Automaton"}}]
    with_trinket = outcomes(mine, enemy)
    check("a trinket body walks in when a slot frees up",
          with_trinket["win"] > without["win"],
          f"{without} -> {with_trinket}")

    # Board-level Avenge: a trinket owns no body, so the count lives on the board
    # and cannot be silenced or killed.
    mine = board(minion(1, 1), minion(1, 1), minion(4, 12))
    enemy = board(minion(2, 2), minion(2, 2), minion(4, 12))
    plain = outcomes(mine, enemy)
    mine.trinket_avenge = [[2, {"buff": {"race": None, "count": 0,
                                         "exclude_self": False,
                                         "attack": 6, "health": 6}}, 0]]
    with_avenge = outcomes(mine, enemy)
    check("trinket avenge fires on the board's own death count",
          with_avenge["win"] > plain["win"], f"{plain} -> {with_avenge}")

    db = get_db(offline=True)
    if db.loaded:
        specs = db.trinket_specs
        automaton = specs.get("BG30_MagicItem_303") or {}
        summon = (automaton.get("when_space") or {}).get("summon") or {}
        check("Automaton Portrait resolves to the real 3/4 body",
              summon.get("attack") == 3 and summon.get("health") == 4, str(summon))
        boom = specs.get("BG30_MagicItem_440") or {}
        grave = ((boom.get("when_space") or {}).get("summon") or {}).get("from_graveyard")
        check("Boom Controller revives a Mech exactly as it fell",
              grave == {"race": "MECHANICAL", "exact": True}, str(grave))


def test_rally_targets() -> None:
    """Rally payloads aimed at the minion that was just attacked."""
    print("rally targets:")

    # Sin'dorei Straight Shot: the Reborn it strips is the difference between a
    # 1/1 wall that stands back up and one that does not.
    wall = board(minion(0, 1, taunt=True, reborn=True), minion(1, 40))
    effects.configure({})
    kept = play(board(minion(1, 60, card_id="SHOT")), wall)
    effects.configure({"SHOT": {"after_attack": {
        "remove_target_keywords": ["taunt", "reborn"]}}})
    stripped = play(board(minion(1, 60, card_id="SHOT")), wall)
    check("stripping reborn stops the wall standing back up",
          kept.attacks > stripped.attacks, f"{kept.attacks} -> {stripped.attacks}")

    effects.configure({"WITCH": {"after_attack": {"set_target_stats": [3, 3], "limit": 1}}})
    check("shrinking the target to 3/3 beats a giant",
          play(board(minion(4, 40, card_id="WITCH")), board(minion(9, 40))).result == "win")
    check("but only once per combat",
          play(board(minion(4, 12, card_id="WITCH")),
               board(minion(9, 40), minion(9, 40))).result == "loss")

    effects.configure({"CANNON": {"after_attack": {
        "target_damage": [{"amount": 5, "mode": "target_neighbours"}]}}})
    check("neighbour damage spills off the target",
          play(board(minion(1, 40, card_id="CANNON")),
               board(minion(1, 3), minion(1, 3), minion(1, 3))).result == "win")

    effects.configure({"DOG": {"after_attack": {"gain_target_attack": True}}})
    check("stealing the target's attack",
          play(board(minion(1, 40, card_id="DOG")), board(minion(6, 12))).result == "win")

    effects.configure({"CORP": {"after_attack": {"destroy_self": True}}})
    check("a minion that destroys itself after swinging",
          play(board(minion(9, 9, card_id="CORP")),
               board(minion(1, 1), minion(1, 1), minion(1, 1))).result == "loss")


def test_positional_and_stolen() -> None:
    """Payloads that name a slot, and Fish of N'Zoth stacking deathrattles."""
    print("positions and stolen deathrattles:")

    effects.configure({"SUPPORT": {"after_attack": {
        "buff": {"race": None, "count": 1, "exclude_self": True,
                 "position": "right", "attack": 4, "health": 4}}}})
    mine = board(minion(1, 20, card_id="SUPPORT"), minion(1, 1))
    check("the buff lands on the minion to the right",
          play(mine, board(minion(2, 2), minion(2, 8))).result == "win")

    effects.configure({
        "BOMB": {"has_deathrattle": True,
                 "deathrattle": {"damage": [{"amount": 10, "mode": "all", "side": "enemy"}]}},
        "MACAW": {"after_attack": {"trigger_deathrattle": {"position": "leftmost",
                                                           "count": 1}}},
    })
    macaw = board(minion(1, 20, card_id="BOMB"), minion(1, 20, card_id="MACAW"))
    check("macaw re-fires the left-most deathrattle without killing it",
          play(macaw, board(minion(2, 9), minion(2, 9))).result == "win")

    effects.configure({
        "BOMB": {"has_deathrattle": True,
                 "deathrattle": {"damage": [{"amount": 10, "mode": "all", "side": "enemy"}]}},
        "FISH": {"on_friendly_death": {"gain_deathrattle": True}},
    })
    fish = board(minion(1, 1, card_id="BOMB"), minion(1, 1, card_id="FISH"))
    check("the fish inherits the deathrattle of the ally that died",
          play(fish, board(minion(2, 9), minion(2, 9))).result in ("win", "tie"))


def test_text_parsing() -> None:
    print("card text -> effects:")
    spec = parse_card_effects(
        "Deathrattle: Summon two 1/1 Beasts.", ["DEATHRATTLE"], {})
    summon = spec.get("deathrattle", {}).get("summon", {})
    check("parses 'Summon two 1/1 Beasts'",
          summon.get("count") == 2 and summon.get("attack") == 1, str(summon))

    index = {"scallywag": {"id": "BGS_061", "name": "Scallywag", "attack": 3,
                           "health": 1, "techLevel": 1, "mechanics": []}}
    spec = parse_card_effects("Deathrattle: Summon 2 Scallywags.", ["DEATHRATTLE"], index)
    summon = spec.get("deathrattle", {}).get("summon", {})
    check("resolves a named token to real stats",
          summon.get("count") == 2 and summon.get("attack") == 3, str(summon))

    spec = parse_card_effects("Start of Combat: Deal 3 damage to two random enemy minions.",
                              [], {})
    damage = (spec.get("start_of_combat", {}).get("damage") or [{}])[0]
    check("parses start-of-combat damage",
          damage.get("amount") == 3 and damage.get("count") == 2, str(damage))

    spec = parse_card_effects("Deathrattle: Deal 4 damage to a random enemy minion, twice.",
                              ["DEATHRATTLE"], {})
    damage = (spec.get("deathrattle", {}).get("damage") or [{}])[0]
    check("parses deathrattle damage and 'twice'",
          damage.get("amount") == 4 and damage.get("count") == 2, str(damage))

    spec = parse_card_effects("Deathrattle: Deal 3 damage to all minions.",
                              ["DEATHRATTLE"], {})
    damage = (spec.get("deathrattle", {}).get("damage") or [{}])[0]
    check("parses board-wide deathrattle damage",
          damage.get("side") == "all" and damage.get("mode") == "all", str(damage))

    spec = parse_card_effects("Deathrattle: Give 2 random friendly minions Divine Shield.",
                              ["DEATHRATTLE"], {})
    grant = (spec.get("deathrattle", {}).get("grant") or [{}])[0]
    check("parses a keyword grant",
          grant.get("keywords") == ["divine_shield"] and grant.get("count") == 2, str(grant))

    spec = parse_card_effects(
        "Deathrattle: Give this minion's Attack to a random friendly minion, twice.",
        ["DEATHRATTLE"], {})
    transfer = spec.get("deathrattle", {}).get("transfer", {})
    check("parses a stat transfer",
          transfer.get("stat") == "attack" and transfer.get("count") == 2, str(transfer))

    spec = parse_card_effects("Deathrattle: Destroy the minion that killed this.",
                              ["DEATHRATTLE"], {})
    check("parses 'destroy the minion that killed this'",
          spec.get("deathrattle", {}).get("destroy_killer") is True, str(spec))

    spec = parse_card_effects("Deathrattle: Summon 2 random Deathrattle minions.",
                              ["DEATHRATTLE"], {})
    summon = spec.get("deathrattle", {}).get("summon", {})
    check("parses a random-pool summon",
          summon.get("count") == 2 and summon.get("pool", {}).get("mechanic") == "DEATHRATTLE",
          str(summon))

    spec = parse_card_effects(
        "Deathrattle: Summon a number of 1/1 Rats equal to this minion's Attack.",
        ["DEATHRATTLE"], {})
    summon = spec.get("deathrattle", {}).get("summon", {})
    check("parses 'equal to this minion's Attack'",
          summon.get("count_from_attack") is True, str(summon))

    spec = parse_card_effects("Frenzy: Give your other minions +2/+2.", [], {})
    buff = spec.get("frenzy", {}).get("buff", {})
    check("parses Frenzy", buff.get("attack") == 2 and buff.get("exclude_self") is True,
          str(spec.get("frenzy")))

    spec = parse_card_effects("Whenever this takes damage, gain Divine Shield.", [], {})
    check("parses 'whenever this takes damage'",
          spec.get("on_damaged", {}).get("gain", {}).get("divine_shield") is True, str(spec))

    spec = parse_card_effects("Whenever this attacks and kills a minion, gain +2/+2.", [], {})
    check("parses a kill trigger",
          spec.get("on_kill", {}).get("gain", {}).get("attack") == 2, str(spec))

    # A deathrattle that only pays off in the tavern must not dent the coverage
    # number the overlay shows.
    spec = parse_card_effects("Deathrattle: Get a Blood Gem.", ["DEATHRATTLE"], {})
    check("tavern-only deathrattle counts as modelled",
          not spec.get("unmodelled"), str(spec))
    spec = parse_card_effects("Deathrattle: Rearrange your opponent's board.",
                              ["DEATHRATTLE"], {})
    check("unparsed combat deathrattle is flagged",
          "deathrattle" in (spec.get("unmodelled") or ()), str(spec))

    spec = parse_card_effects("Avenge (2): Give your Beasts +1/+1.", [], {})
    check("parses avenge with a tribal buff",
          spec.get("avenge", {}).get("buff", {}).get("race") == "BEAST", str(spec))

    spec = parse_card_effects(
        "Also damages the minions next to whomever it attacks.", [], {})
    check("detects cleave", spec.get("cleave") is True, str(spec))


def test_new_wordings() -> None:
    """The card wordings this pass taught the parser to read."""
    print("newly parsed wordings:")

    spec = parse_card_effects("Battlecry, Deathrattle, and Rally: Give your other "
                              "minions +2/+2.", ["DEATHRATTLE"], {})
    check("one payload shared by several triggers reaches all of them",
          spec.get("deathrattle", {}).get("buff", {}).get("attack") == 2
          and spec.get("after_attack", {}).get("buff", {}).get("attack") == 2,
          str(spec))

    spec = parse_card_effects("Rally: Cast Queen's Command.", [], {},
                              {"queen's command": "Give your minions +2/+2."})
    check("a named spell is inlined into the trigger that casts it",
          spec.get("after_attack", {}).get("buff", {}).get("health") == 2, str(spec))

    spec = parse_card_effects("Rally: Remove Reborn and Taunt from the target.", [], {})
    check("keywords stripped off the attack target",
          spec["after_attack"]["remove_target_keywords"] == ["reborn", "taunt"], str(spec))

    spec = parse_card_effects("Rally: Set the target's stats to 3/3. "
                              "(Once per combat.)", [], {})
    check("the target's stats set, once",
          spec["after_attack"]["set_target_stats"] == [3, 3]
          and spec["after_attack"]["limit"] == 1, str(spec))

    spec = parse_card_effects("Rally: Give the minion to the right of this +2/+2.", [], {})
    check("a buff aimed at a slot rather than a tribe",
          spec["after_attack"]["buff"]["position"] == "right", str(spec))

    spec = parse_card_effects("Rally: Trigger your left-most Deathrattle twice.", [], {})
    check("re-firing another minion's deathrattle",
          spec["after_attack"]["trigger_deathrattle"] == {"position": "leftmost",
                                                          "count": 2}, str(spec))

    spec = parse_card_effects("Deathrattle: Summon the highest-Health Murloc from "
                              "your hand for this combat only.", ["DEATHRATTLE"], {})
    check("hand summons can pick by Health",
          spec["deathrattle"]["summon_from_hand"] == {"count": 1, "by": "health",
                                                      "race": "MURLOC"}, str(spec))

    spec = parse_card_effects("Deathrattle: Summon and get 4 random Pirates.",
                              ["DEATHRATTLE"], {})
    check("a plural tribe still resolves",
          spec["deathrattle"]["summon"]["pool"]["race"] == "PIRATE"
          and spec["deathrattle"]["summon"]["count"] == 4, str(spec))

    spec = parse_card_effects("Rally: This plays 2 permanent Blood Gems on all your "
                              "other minions.", [], {})
    check("permanent blood gems on the rest of the board",
          spec["after_attack"]["buff"] == {"race": None, "count": 0,
                                           "exclude_self": True,
                                           "attack": 2, "health": 2}, str(spec))

    spec = parse_card_effects("Whenever you summon a minion in combat, give it "
                              "+3/+3.", [], {})
    check("\"in combat\" reads the same as \"during combat\"",
          spec["on_summon"]["buff_summoned"] == {"attack": 3, "health": 3}, str(spec))

    spec = parse_card_effects("After a different friendly Deathrattle minion dies in "
                              "combat, gain its Deathrattle.", [], {})
    check("inheriting a dead ally's deathrattle",
          spec["on_friendly_death"].get("gain_deathrattle") is True, str(spec))

    from hsbg.carddb import _hero_power_spec
    check("hero powers that act inside the fight",
          _hero_power_spec("After a friendly minion kills an enemy, give it +1 Attack "
                           "permanently.") == {"on_kill_buff": {"attack": 1, "health": 0}}
          and _hero_power_spec("When you have space in combat, summon a copy of your "
                               "highest-Health minion. (Unlocks on Turn 7.)")
          == {"when_space": {"copy": "health", "unlock_turn": 7}})


def test_module_attributes() -> None:
    """Every ``module.name`` we write about our own modules must exist.

    Moving the "?" pin geometry from render to hover left one ``render.MARK_SIZE``
    behind, and nothing caught it: pyflakes does not check attributes on a
    module, and the line only runs when a real overlay window is built — so it
    surfaced as a crash on launch. This walks the ASTs instead and asks the
    imported module itself.
    """
    import ast
    import importlib

    print("cross-module references:")
    root = Path(__file__).resolve().parent.parent / "hsbg"
    bad: list[str] = []
    checked = 0
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        package = ".".join(["hsbg", *path.relative_to(root).parts[:-1]])
        aliases: dict[str, object] = {}
        for node in ast.walk(tree):
            # "from . import render", "from . import pool as pool_module"
            if isinstance(node, ast.ImportFrom) and node.level and not node.module:
                for alias in node.names:
                    target = f"{package}.{alias.name}"
                    try:
                        aliases[alias.asname or alias.name] = importlib.import_module(target)
                    except ImportError:
                        continue
        if not aliases:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                continue
            module = aliases.get(node.value.id)
            if module is None:
                continue
            checked += 1
            if not hasattr(module, node.attr):
                bad.append(f"{path.name}:{node.lineno} {node.value.id}.{node.attr}")
    check(f"all {checked} references resolve", not bad, "; ".join(bad[:5]))


def test_parser() -> None:
    print("power.log parser:")
    lines = [
        "D 00:00:00.0000000 GameState.DebugPrintPower() - CREATE_GAME",
        "D 00:00:00.0000000 GameState.DebugPrintPower() -     GameEntity EntityID=1",
        "D 00:00:00.0000000 GameState.DebugPrintPower() -         tag=TURN value=3",
        "D 00:00:00.0000000 GameState.DebugPrintPower() -     Player EntityID=2 PlayerID=5 "
        "GameAccountId=[hi=123 lo=456]",
        "D 00:00:00.0000000 GameState.DebugPrintPower() -     Player EntityID=3 PlayerID=13 "
        "GameAccountId=[hi=0 lo=0]",
        "D 00:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_BATTLEGROUNDS",
        "D 00:00:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=50 "
        "CardID=BG_TEST",
        "D 00:00:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=MINION",
        "D 00:00:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=5",
        "D 00:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=PLAY",
        "D 00:00:00.0000000 GameState.DebugPrintPower() -     tag=ZONE_POSITION value=1",
        "D 00:00:00.0000000 GameState.DebugPrintPower() -     tag=ATK value=4",
        "D 00:00:00.0000000 GameState.DebugPrintPower() -     tag=HEALTH value=7",
        "D 00:00:00.0000000 GameState.DebugPrintPower() -     tag=DIVINE_SHIELD value=1",
    ]
    state = BattlegroundsState()
    state.feed_lines(lines)
    check("identifies the local player", state.my_player_id == 5, str(state.my_player_id))
    check("identifies the ghost player", state.ghost_player_id == 13,
          str(state.ghost_player_id))
    check("recognises battlegrounds", state.is_battlegrounds)
    check("reads the turn", state.turn == 3, str(state.turn))

    my_board = state.current_my_board()
    check("finds one minion on our board", len(my_board.minions) == 1,
          str(my_board.minions))
    if my_board.minions:
        m = my_board.minions[0]
        check("reads its stats and keywords",
              (m.attack, m.health, m.divine_shield) == (4, 7, True),
              f"{m.attack}/{m.health} ds={m.divine_shield}")


def test_match_end() -> None:
    """Nothing live may survive the match it belongs to."""
    print("end of match:")
    lines = [
        "D 00:00:00.0000000 GameState.DebugPrintPower() - CREATE_GAME",
        "D 00:00:00.0000000 GameState.DebugPrintPower() -     GameEntity EntityID=1",
        "D 00:00:00.0000000 GameState.DebugPrintPower() -         tag=TURN value=9",
        "D 00:00:00.0000000 GameState.DebugPrintPower() -     Player EntityID=2 PlayerID=5 "
        "GameAccountId=[hi=123 lo=456]",
        "D 00:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_BATTLEGROUNDS",
    ]
    state = BattlegroundsState()
    state.feed_lines(lines)
    check("a match in progress is live", state.game_active and state.in_gameplay)
    check("an unwritten scene log still counts as gameplay", state.in_gameplay,
          state.scene)

    state.feed_line("D 00:00:00.0000000 GameState.DebugPrintPower() - TAG_CHANGE "
                    "Entity=GameEntity tag=STATE value=COMPLETE")
    check("notices the lobby being decided", state.game_over)
    check("but keeps the match on screen for its last fight", state.game_active)

    state.set_scene("BACON")
    check("the menu ends the match", not state.game_active, str(state.game_active))
    check("and clears the live combat", state.current_combat is None
          and state.phase == "idle", state.phase)

    # The other half of the same bug: the app starts while the player sits in
    # the menu, so the log replay hands us a finished match.
    stale = BattlegroundsState()
    stale.set_scene("BACON")
    stale.feed_lines(lines)
    check("a finished match replayed in the menu stays off", not stale.in_gameplay,
          stale.scene)


def test_marks_toggle() -> None:
    """The "?" pins are off until asked for, and asking is one click."""
    print("hover pins:")
    from hsbg.config import Settings
    from hsbg.ui import render
    from hsbg.viewmodel import OpponentView, ViewModel

    check("pins are off by default", not Settings().show_hover_marks)

    vm = ViewModel(connected=True, turn=4, phase="shop")
    vm.leaderboard = [OpponentView(player_id=i, name=f"p{i}", place=i, is_me=(i == 1))
                      for i in range(1, 9)]
    _, _, spots = render.build_panels(vm)
    toggle = [s for s in spots if s[0] == render.KEY_MARKS]
    check("the lobby header carries a switch", len(toggle) == 1, str(spots[:2]))
    if toggle:
        key, x, y, w, h = toggle[0]
        hit = next((k for k, hx, hy, hw, hh in spots
                    if hx <= x + w / 2 <= hx + hw and hy <= y + h / 2 <= hy + hh), None)
        check("clicking it toggles instead of collapsing", hit == render.KEY_MARKS,
              str(hit))

    vm.show_marks = True
    labels = [op[3] for op in render.build_panels(vm)[0]
              if op[0] == "text" and str(op[3]).startswith("?")]
    check("the switch reads out its state", labels == ["? on"], str(labels))

    marks, size, _ = render.build_hero_marks(vm.leaderboard, 40.0, 400.0)
    check("pins are drawn for everyone but us",
          sum(1 for op in marks if op[0] == "text" and op[3] == "?") == 7,
          str(size))

    test_pin_hit_test()
    test_marks_survive_collapse()


def test_language_detection() -> None:
    """Which language the game is in — the question both card names and our own
    labels hang off. Having Russian *installed* is not the same as playing it."""
    print("game language:")
    import tempfile
    from hsbg import config

    def field(number: int, payload: bytes) -> bytes:
        return bytes([number << 3 | 2, len(payload)]) + payload

    # Battle.net's record: English selected, both languages on disk.
    settings = (field(1, b"/Applications/Hearthstone")
                + field(6, b"enUS") + field(7, b"enUS")
                + field(8, field(1, b"ruRU")) + field(8, field(1, b"enUS")))
    product_db = field(1, b"hs_beta") + field(3, settings)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / ".product.db"
        path.write_bytes(product_db)
        saved = config.PRODUCT_DB_PATHS[:]
        config.PRODUCT_DB_PATHS[:] = [path]
        try:
            found = config._locale_from_product_db()
        finally:
            config.PRODUCT_DB_PATHS[:] = saved
        check("the selected language beats the installed ones", found == "enUS", found)

        launch = root / "Hearthstone_2026_08_20_01_30_43"
        launch.mkdir()
        (launch / "Hearthstone.log").write_text(
            "I 01:30:43.5 Initialize\nI 01:30:43.5 SetLocale: ruRU\n", "utf-8")
        saved_roots = config.LOG_ROOTS[:]
        config.LOG_ROOTS[:] = [root]
        try:
            found = config._locale_from_logs()
        finally:
            config.LOG_ROOTS[:] = saved_roots
        check("the game's own log has the last word", found == "ruRU", found)

    check("an unknown locale is not passed on", config._known("xxXX") == "")
    check("enGB reads as enUS", config._known("enGB") == "enUS")


def test_labels_follow_the_language() -> None:
    """An English client must not be handed Russian labels — and the other way."""
    print("interface language:")
    from hsbg import i18n
    from hsbg.ui import render
    from hsbg.viewmodel import HeroChoiceView, OpponentView, ViewModel

    missing = [key for key, entry in i18n.STRINGS.items()
               if set(entry) != {i18n.RU, i18n.EN}]
    check("every label exists in both languages", not missing, str(missing[:3]))

    def drawn(language: str) -> list[str]:
        vm = ViewModel(connected=True, turn=7, phase="shop", my_health=25,
                       my_tier=3, language=language)
        vm.leaderboard = [OpponentView(player_id=1, name="p1", place=1, tier=2)]
        vm.opponents = [OpponentView(player_id=1, name="p1", turn_seen=5)]
        vm.hero_choices = [HeroChoiceView(name="Bob", personal_games=4,
                                          personal_avg=3.5)]
        ops = render.build_panels(vm)[0] + render.build_main_bar(vm)[0]
        return [str(op[3]) for op in ops if op[0] == "text"]

    cyrillic = [text for text in drawn("enUS")
                if any("а" <= c.lower() <= "я" for c in text)]
    check("an English client sees no Russian", not cyrillic, str(cyrillic[:3]))
    check("a Russian client still sees Russian",
          any("ход" in text for text in drawn("ruRU")), str(drawn("ruRU")[:3]))
    # Anything we have no translation for falls back to English, not to silence.
    check("an unhandled locale falls back to English",
          i18n.strings("deDE")("status.tavern") == "Tavern")


def test_marks_survive_collapse() -> None:
    """Collapsing the panels must not take the "?" pins with them."""
    from hsbg.config import Settings
    from hsbg.ui.overlay import OverlayController
    from hsbg.viewmodel import ViewModel

    settings = Settings()
    settings.show_hover_marks = True
    controller = OverlayController.__new__(OverlayController)
    controller.settings = settings
    controller.model = ViewModel(connected=True, turn=4, phase="shop")

    check("pins are up while expanded", controller._marks_active())
    controller.model.hidden = True
    check("and stay up once collapsed", controller._marks_active())
    settings.show_hover_marks = False
    check("their own switch is what turns them off",
          not controller._marks_active())


def test_pin_hit_test() -> None:
    """Only the pin opens an opponent's board — not the strip beside it."""
    from hsbg.ui.hover import DEFAULT_ZONES, hit_test, mark_layout
    from hsbg.ui.hswindow import WindowRect

    rect = WindowRect(x=0.0, y=0.0, width=1440.0, height=900.0)
    left, top, right, bottom = DEFAULT_ZONES["leaderboard"]
    rail_w, rail_h = (right - left) * rect.width, (bottom - top) * rect.height
    mark_x, size, slot_h = mark_layout(rail_w, rail_h)

    def at(x_in_rail: float, slot: int):
        y = top * rect.height + slot * slot_h + slot_h / 2.0
        return hit_test((left * rect.width + x_in_rail, y), rect, DEFAULT_ZONES,
                        marks_enabled=True)

    centre = mark_x + size / 2.0
    check("the pin answers", (at(centre, 3) or None) and at(centre, 3).kind == "hero"
          and at(centre, 3).index == 3, str(at(centre, 3)))
    check("the portrait left of it does not", at(2.0, 3) is None, str(at(2.0, 3)))
    check("and neither does the pin with pins switched off",
          hit_test((left * rect.width + centre, top * rect.height + slot_h * 3.5),
                   rect, DEFAULT_ZONES, marks_enabled=False) is None)


def main() -> int:
    test_combat()
    test_triggers()
    test_rally_and_hand()
    test_scaling_packages()
    test_keywords()
    test_trinkets()
    test_rally_targets()
    test_positional_and_stolen()
    test_text_parsing()
    test_new_wordings()
    test_module_attributes()
    test_parser()
    test_match_end()
    test_marks_toggle()
    test_language_detection()
    test_labels_follow_the_language()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for failure in FAILURES:
            print("  -", failure)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
