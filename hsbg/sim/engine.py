"""Battlegrounds combat resolution.

One ``resolve_combat`` call plays out a single randomised combat. The caller
(:mod:`hsbg.sim.runner`) repeats it a few thousand times to get a distribution.

Everything that can be read straight off the log — stats, Divine Shield, Taunt,
Poisonous/Venomous, Reborn, Windfury — is modelled exactly. Card-specific
behaviour (deathrattles, Frenzy, Avenge, kill and damage triggers,
start-of-combat, ...) comes from the registry in :mod:`hsbg.sim.effects`; minions
with no registered effect simply fight with their printed stats.

The engine only supplies the facts those triggers need — who damaged whom, who
landed the killing blow, what has died so far — and leaves the payloads to the
effects module.
"""
from __future__ import annotations

import random
from typing import Optional

from .model import SimBoard, SimMinion

MAX_TOTAL_ATTACKS = 500      # runaway-combat guard; real combats are far shorter
MAX_TRIGGER_DEPTH = 40
MAX_ROUNDS = 1200            # hard stop: no card interaction may hang the overlay


class Outcome:
    __slots__ = ("result", "damage", "attacks")

    def __init__(self, result: str, damage: int, attacks: int = 0):
        self.result = result      # "win" | "loss" | "tie" from board A's view
        self.damage = damage      # damage the winner deals to the loser's hero
        self.attacks = attacks


class Combat:
    """A single playthrough. Owns both boards and the RNG."""

    def __init__(self, board_a: SimBoard, board_b: SimBoard,
                 rng: random.Random, effects=None, damage_cap: int = 0):
        self.boards = (board_a, board_b)
        self.rng = rng
        self.damage_cap = damage_cap
        self.attacks = 0
        self._depth = 0
        if effects is None:
            from . import effects as effects_module
            effects = effects_module
        self.effects = effects

    # -------------------------------------------------------------- helpers

    def opponent_of(self, board: SimBoard) -> SimBoard:
        return self.boards[1] if board is self.boards[0] else self.boards[0]

    # ---------------------------------------------------------------- setup

    def run(self) -> Outcome:
        self.effects.apply_static_all(self)
        # When the snapshot was taken just before the first attack, the game has
        # already resolved every start-of-combat effect into these stats, so
        # replaying them here would count them twice.
        if not (self.boards[0].post_start_of_combat
                and self.boards[1].post_start_of_combat):
            self.effects.run_start_of_combat(self)
        self.resolve_deaths()

        side = self._pick_first_attacker()
        passes = 0
        rounds = 0
        # Two independent stops: a pass counter for genuinely finished combats,
        # and a hard round cap so no card interaction can ever hang the overlay.
        while passes < 2 and self.attacks < MAX_TOTAL_ATTACKS and rounds < MAX_ROUNDS:
            rounds += 1
            board = self.boards[side]
            if self.boards[0].is_empty or self.boards[1].is_empty:
                break
            if self._take_turn(board):
                passes = 0
            else:
                passes += 1
            side ^= 1

        return self._score()

    def _pick_first_attacker(self) -> int:
        a, b = len(self.boards[0].alive), len(self.boards[1].alive)
        if a > b:
            return 0
        if b > a:
            return 1
        return self.rng.randint(0, 1)

    def _score(self) -> Outcome:
        a_empty, b_empty = self.boards[0].is_empty, self.boards[1].is_empty
        if a_empty == b_empty:
            # Both wiped, or the loop stopped with minions still standing on
            # both sides — a stand-off where nothing left can attack, or one of
            # the runaway guards. Neither hero is damaged in any of those, which
            # is a draw; awarding it to board A invented a win out of a stalemate.
            return Outcome("tie", 0, self.attacks)
        winner = self.boards[1] if a_empty else self.boards[0]
        damage = winner.damage_score()
        if self.damage_cap:
            damage = min(damage, self.damage_cap)
        return Outcome("loss" if a_empty else "win", damage, self.attacks)

    # ----------------------------------------------------------------- turn

    def _next_attacker(self, board: SimBoard) -> Optional[SimMinion]:
        """Minions attack left to right, the pointer surviving across turns."""
        living = board.minions
        n = len(living)
        if n == 0:
            return None
        if board.attack_index >= n:
            board.attack_index = 0
        for step in range(n):
            idx = (board.attack_index + step) % n
            m = living[idx]
            if m.can_attack:
                board.attack_index = idx + 1
                return m
        return None

    def _take_turn(self, board: SimBoard) -> bool:
        attacker = self._next_attacker(board)
        if attacker is None:
            return False

        acted = False
        for _ in range(attacker.attacks_per_turn):
            if attacker.dead or not attacker.can_attack:
                break
            if self.opponent_of(board).is_empty:
                break
            if not self.perform_attack(board, attacker):
                break
            acted = True
        # An attacker that found no legal target (everything left is Stealthed)
        # must count as a pass, or the combat loop never advances.
        return acted

    # --------------------------------------------------------------- attack

    def choose_target(self, defenders: SimBoard) -> Optional[SimMinion]:
        taunts = defenders.taunts()
        pool = taunts if taunts else defenders.targetable()
        if not pool:
            return None
        return pool[self.rng.randrange(len(pool))]

    def perform_attack(self, board: SimBoard, attacker: SimMinion) -> bool:
        defenders = self.opponent_of(board)
        target = self.choose_target(defenders)
        if target is None:
            return False

        self.attacks += 1
        attacker.attacks_taken += 1
        self.effects.on_before_attack(self, board, attacker, target)
        if attacker.dead or target.dead:
            self.resolve_deaths()
            return True

        atk_damage = attacker.attack
        def_damage = target.attack

        if attacker.cleave:
            idx = defenders.index_of(target)
            for neighbour_idx in (idx - 1, idx + 1):
                if 0 <= neighbour_idx < len(defenders.minions):
                    neighbour = defenders.minions[neighbour_idx]
                    if not neighbour.dead:
                        self.deal_damage(neighbour, atk_damage, attacker, defenders)

        self.deal_damage(target, atk_damage, attacker, defenders)
        self.deal_damage(attacker, def_damage, target, board)

        # Both blows land together, so a trade can fire both sides' kill triggers.
        if target.health <= 0:
            attacker.kill_count += 1
            self.effects.on_kill(self, board, attacker, target, attacking=True)
            self.effects.hero_on_kill(board, attacker)
        if attacker.health <= 0:
            target.kill_count += 1
            self.effects.on_kill(self, defenders, target, attacker, attacking=False)
            self.effects.hero_on_kill(defenders, target)

        self.effects.on_after_attack(self, board, attacker, target)
        self.resolve_deaths()
        return True

    # --------------------------------------------------------------- damage

    def deal_damage(self, target: SimMinion, amount: int,
                    source: Optional[SimMinion], target_board: SimBoard) -> bool:
        """Returns True if the target actually lost health."""
        if amount <= 0 or target.dead or target.immune:
            return False
        if target.divine_shield:
            target.divine_shield = False
            self.effects.on_divine_shield_lost(self, target_board, target)
            return False
        target.health -= amount
        if source is not None and (source.poisonous or source.venomous):
            target.health = min(target.health, 0)
            if source.venomous:
                source.venomous = False
        target.damaged_count += 1
        if target.health <= 0 and source is not None:
            # Remembered for "Deathrattle: ... the minion that killed this".
            target.killer = source
        self.effects.on_damaged(self, target_board, target, source, amount)
        return True

    # ---------------------------------------------------------------- deaths

    def resolve_deaths(self) -> None:
        if self._depth > MAX_TRIGGER_DEPTH:
            return
        self._depth += 1
        try:
            while True:
                dying: list[tuple[SimBoard, SimMinion, int]] = []
                for board in self.boards:
                    for idx, m in enumerate(board.minions):
                        if not m.dead and m.health <= 0:
                            dying.append((board, m, idx))
                if not dying:
                    break
                for board, minion, _ in dying:
                    minion.dead = True
                    board.deaths_this_combat += 1
                    board.graveyard.append(minion)
                for board, minion, idx in dying:
                    self._on_death(board, minion, idx)
                # Dead bodies leave the board once their triggers have resolved.
                for board in self.boards:
                    if any(m.dead for m in board.minions):
                        self._compact(board)
                for board in self.boards:
                    self._fill_free_slot(board)
        finally:
            self._depth -= 1

    def _fill_free_slot(self, board: SimBoard) -> None:
        """Everything worded "when you have space, summon ...".

        Drek'Thar and Vanndar Stormpike spend their hero power the instant a slot
        opens; Automaton Portrait and Boom Controller do the same from a trinket.
        Each entry fires once, and only while there is room — which is why an
        unmodelled one shifted every slot index after it and read as an
        attack-order bug in tools/divergence.py.
        """
        while board.space_queue and len(board.alive) < SimBoard.MAX_MINIONS:
            spec = board.space_queue.pop(0)
            if "copy" in spec:
                self._summon_board_copy(board, spec["copy"])
            else:
                self.effects.run_space_summon(self, board, spec)

    def _summon_board_copy(self, board: SimBoard, by: str) -> None:
        """"Summon a copy of your highest-Attack/Health minion"."""
        living = board.alive
        if not living:
            return
        key = (lambda m: (m.max_health, m.attack)) if by == "health" \
            else (lambda m: (m.attack, m.max_health))
        source = max(living, key=key)
        copy = source.clone()
        copy.entity_id = 0
        copy.attacks_taken = 0
        copy.damaged_count = 0
        copy.kill_count = 0
        copy.killer = None
        copy.avenge_counter = 0
        copy.health = copy.max_health
        self.summon(board, copy, len(board.minions))

    def _compact(self, board: SimBoard) -> None:
        removed_before_pointer = sum(
            1 for i, m in enumerate(board.minions) if m.dead and i < board.attack_index)
        board.minions = [m for m in board.minions if not m.dead]
        board.attack_index = max(0, board.attack_index - removed_before_pointer)

    def _on_death(self, board: SimBoard, minion: SimMinion, index: int) -> None:
        self.effects.on_death(self, board, minion, index)
        if minion.reborn:
            self.summon_reborn(board, minion, index)
        self.effects.on_any_death(self, board, minion)

    def summon_reborn(self, board: SimBoard, minion: SimMinion, index: int) -> None:
        copy = minion.clone()
        copy.dead = False
        copy.reborn = False
        copy.health = 1
        copy.max_health = 1
        copy.attacks_taken = 0
        copy.damaged_count = 0
        copy.kill_count = 0
        copy.killer = None
        copy.avenge_counter = 0
        copy.summoned_in_combat = True
        # The body comes back exactly as it stood, minus one life and minus the
        # Reborn itself. Restoring the card's printed Divine Shield here was
        # tried and measured: it cost 5 points of accuracy over 207 real
        # combats, so the game evidently does not give it back.
        self.summon(board, copy, index)

    # --------------------------------------------------------------- summon

    def summon(self, board: SimBoard, minion: SimMinion, index: int) -> bool:
        minion.summoned_in_combat = True
        # Every mid-fight body funnels through here, which is exactly what a
        # hero power like "give +1/+2 and Taunt to minions you summon during
        # combat" applies to.
        buff = board.summon_buff
        if buff:
            minion.attack += int(buff.get("attack", 0))
            minion.health += int(buff.get("health", 0))
            minion.max_health = max(minion.max_health, minion.health)
            for flag in ("taunt", "divine_shield", "windfury"):
                if buff.get(flag):
                    setattr(minion, flag, True)
        # "Your Beetles have +5/+5 this game" — the same funnel, but the order
        # standing on the board rather than on the hero.
        self.effects.apply_token_buff(board, minion)
        # Insert to the right of the dead minion's slot, skipping corpses.
        target_index = 0
        for i, m in enumerate(board.minions):
            if i >= index:
                target_index = i
                break
        else:
            target_index = len(board.minions)
        if not board.insert(minion, target_index):
            return False
        self.effects.on_summon(self, board, minion)
        return True


def resolve_combat(board_a: SimBoard, board_b: SimBoard, rng: random.Random,
                   damage_cap: int = 0) -> Outcome:
    return Combat(board_a.clone(), board_b.clone(), rng, damage_cap=damage_cap).run()
