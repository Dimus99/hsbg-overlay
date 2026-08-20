#!/usr/bin/env python3
"""Replay recorded combats swing by swing and report where the engine diverges.

    python3 tools/divergence.py [logs...] [--limit N] [--show N]

Scoring the simulator against final outcomes (``tools/accuracy.py``) says *how
often* it is wrong but never *why*: a 7x7 fight has forty swings and one bad
rule anywhere in them flips the result. Power.log, though, names both sides of
every single swing (``PROPOSED_ATTACKER`` / ``PROPOSED_DEFENDER`` inside each
ATTACK block) — the game's own move list.

So this tool pins the simulator to that move list. At each step it asks the
engine who it would swing next, compares that with who actually swung, then
*forces* the real choice so the replay stays on the real trajectory. Two error
classes fall out, and they need completely different fixes:

``order``  the engine picked a different attacker than the game did — the attack
           pointer, summon placement or windfury bookkeeping is wrong.
``state``  the engine has the real attacker or its target already dead — a
           missing effect, or damage resolved differently.

Bodies summoned mid-fight get no id in the simulator, so they are bound to the
log's ids by card, in summon order. When that cannot be done the replay *stops*
rather than reporting a divergence: leaving the trackable region is not an
engine bug, and counting it as one would drown the real findings.

Only the first divergence in a fight means anything — everything after it is
downstream of a board that has already drifted — so each combat contributes at
most one finding.
"""
from __future__ import annotations

import argparse
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hsbg.carddb import _clean, get_db                  # noqa: E402
from hsbg.config import LOG_ROOTS                       # noqa: E402
from hsbg.gamestate import BattlegroundsState           # noqa: E402
from hsbg.logfiles import read_log_file                 # noqa: E402
from hsbg.sim import effects as effects_module          # noqa: E402
from hsbg.sim.engine import Combat                      # noqa: E402
from hsbg.sim.model import SimBoard, SimMinion, board_from_snapshot  # noqa: E402


class Stop(Exception):
    """The replay left the region the log lets us follow. Not a bug."""

    def __init__(self, reason: str, step: int):
        super().__init__(reason)
        self.reason = reason
        self.step = step


class Divergence(Exception):
    """The engine and the game disagree about what the rules do."""

    def __init__(self, kind: str, detail: str, step: int):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.step = step


def _base(card_id: str) -> str:
    return card_id[:-2] if card_id.endswith("_G") else card_id


class ScriptedCombat(Combat):
    """A combat driven by the log's move list instead of by the RNG."""

    def __init__(self, board_a: SimBoard, board_b: SimBoard, trace: list, **kw):
        super().__init__(board_a, board_b, random.Random(0), **kw)
        self.script = [t for t in trace if t[0] == "attack"]
        # log entity id -> card id, for every body the game summoned mid-fight
        self.summoned_cards = {t[1]: t[2] for t in trace if t[0] == "summon"}
        self.step = 0
        self._forced: Optional[SimMinion] = None
        # Whose turn it is on each board, and how many swings it has spent.
        # Windfury lives in ``Combat._take_turn``, which the scripted replay
        # bypasses, so the "who swings next" comparison has to reproduce it or
        # every Windfury minion's second swing reads as an order bug.
        self._turn: dict[int, list] = {}
        # Card the replay tripped over, so the run can rank what to model next.
        self.missing_card: str = ""

    # -- lining log ids up with simulated bodies ---------------------------

    def find(self, entity_id: int, side: Optional[SimBoard] = None):
        for board in self.boards:
            for m in board.minions:
                if m.entity_id == entity_id:
                    return board, m
        return None, None

    def bind(self, entity_id: int, side: Optional[SimBoard]):
        """Attach a log id to a body the simulator summoned during the fight.

        Summons carry no id of their own, so they are matched by card — in
        summon order, on the side the swing implies. Good enough to keep
        following a fight past its first deathrattle, which is where the
        interesting divergences start.
        """
        card = self.summoned_cards.get(entity_id)
        if card is None:
            return None, None
        boards = [side] if side is not None else list(self.boards)
        for board in boards:
            for m in board.minions:
                if m.entity_id == 0 and not m.dead and _base(m.card_id) == _base(card):
                    m.entity_id = entity_id
                    return board, m
        return None, None

    def locate(self, entity_id: int, side: Optional[SimBoard], role: str):
        board, minion = self.find(entity_id)
        if minion is None:
            board, minion = self.bind(entity_id, side)
        if minion is None:
            if entity_id in self.summoned_cards:
                self.missing_card = self.summoned_cards[entity_id]
                raise Stop(f"{role} {entity_id} is a mid-fight summon the "
                           f"simulator never produced "
                           f"({self.missing_card})", self.step)
            raise Stop(f"{role} {entity_id} is not a minion on either board "
                       f"(hero blow, or a body from the setup block)", self.step)
        return board, minion

    # -- driving -----------------------------------------------------------

    def run_scripted(self) -> int:
        self.effects.apply_static_all(self)
        if not (self.boards[0].post_start_of_combat
                and self.boards[1].post_start_of_combat):
            self.effects.run_start_of_combat(self)
        self.resolve_deaths()

        while self.step < len(self.script):
            _, attacker_id, target_id = self.script[self.step]

            # Resolve whichever side we can name first; it fixes the other.
            board, attacker = self.find(attacker_id)
            defenders, target = self.find(target_id)
            if attacker is None and defenders is not None:
                board, attacker = self.locate(attacker_id,
                                              self.opponent_of(defenders), "attacker")
            elif attacker is None:
                board, attacker = self.locate(attacker_id, None, "attacker")
            if target is None:
                defenders, target = self.locate(target_id,
                                                self.opponent_of(board), "target")

            if attacker.dead:
                raise Divergence("state", f"the game swung with {_label(attacker)}, "
                                          f"which the simulator already has dead",
                                 self.step)
            if target.dead:
                raise Divergence("state", f"{_label(attacker)} attacked "
                                          f"{_label(target)}, which the simulator "
                                          f"already has dead", self.step)
            if defenders is board:
                raise Stop("attacker and target resolved to the same board",
                           self.step)

            guess = self._expected_attacker(board)
            if guess is not None and guess is not attacker:
                raise Divergence(
                    "order",
                    f"engine would swing {_label(guess)} from slot "
                    f"{board.index_of(guess)}; the game swung {_label(attacker)} "
                    f"from slot {board.index_of(attacker)}", self.step)

            self._forced = target
            try:
                board.attack_index = board.index_of(attacker) + 1
                attacker.attacks_taken += 1
                self._note_swing(board, attacker)
                self._resolve_forced_attack(board, attacker, target)
            finally:
                self._forced = None
            self.step += 1
        return self.step

    # -- reproducing _take_turn's bookkeeping ------------------------------

    def _expected_attacker(self, board: SimBoard) -> Optional[SimMinion]:
        """Who the engine would swing next on this board.

        A minion in the middle of its turn keeps swinging (Windfury twice,
        Mega-Windfury four times) before the pointer moves on, so that case has
        to be checked before asking ``_next_attacker`` at all.
        """
        turn = self._turn.get(id(board))
        if turn is not None:
            actor, used = turn
            if (not actor.dead and actor.can_attack
                    and used < actor.attacks_per_turn
                    and not self.opponent_of(board).is_empty):
                return actor
        return self._next_attacker_preview(board)

    def _note_swing(self, board: SimBoard, attacker: SimMinion) -> None:
        turn = self._turn.get(id(board))
        if turn is not None and turn[0] is attacker:
            turn[1] += 1
        else:
            self._turn[id(board)] = [attacker, 1]

    def _next_attacker_preview(self, board: SimBoard) -> Optional[SimMinion]:
        """Who the engine would pick, without moving the pointer."""
        saved = board.attack_index
        try:
            return self._next_attacker(board)
        finally:
            board.attack_index = saved

    def _resolve_forced_attack(self, board: SimBoard, attacker: SimMinion,
                               target: SimMinion) -> None:
        """``perform_attack`` with the target already decided by the log."""
        defenders = self.opponent_of(board)
        self.attacks += 1
        self.effects.on_before_attack(self, board, attacker, target)
        if attacker.dead or target.dead:
            self.resolve_deaths()
            return

        atk_damage, def_damage = attacker.attack, target.attack
        if attacker.cleave:
            idx = defenders.index_of(target)
            for neighbour in (idx - 1, idx + 1):
                if 0 <= neighbour < len(defenders.minions):
                    body = defenders.minions[neighbour]
                    if not body.dead:
                        self.deal_damage(body, atk_damage, attacker, defenders)
        self.deal_damage(target, atk_damage, attacker, defenders)
        self.deal_damage(attacker, def_damage, target, board)
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

    def choose_target(self, defenders: SimBoard) -> Optional[SimMinion]:
        """Triggers that ask for a target during a forced swing get the real one."""
        if self._forced is not None and not self._forced.dead:
            return self._forced
        return super().choose_target(defenders)


def _label(m: SimMinion) -> str:
    return f"{m.name or m.card_id} {m.attack}/{m.health} (id {m.entity_id})"


def all_logs() -> list[Path]:
    out: list[Path] = []
    for root in LOG_ROOTS:
        if root.is_dir():
            out.extend(sorted(root.glob("Hearthstone_*/Power.log")))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="*")
    ap.add_argument("--limit", type=int, default=0, help="stop after N combats")
    ap.add_argument("--show", type=int, default=20, help="how many findings to print")
    ap.add_argument("--min-swings", type=int, default=1,
                    help="ignore combats shorter than this")
    args = ap.parse_args()

    paths = [Path(p) for p in args.logs] or all_logs()
    if not paths:
        print("No logs found.", file=sys.stderr)
        return 1

    db = get_db()
    effects_module.configure(db.derive_effects() if db.loaded else {},
                             db.derive_pool() if db.loaded else [])
    powers, trinkets = db.hero_power_specs, db.trinket_specs

    combats = []
    for path in paths:
        print(f"  parsing {path.parent.name}", flush=True)
        state = BattlegroundsState()
        state.trace_combat = True
        try:
            state.feed_lines(read_log_file(path))
        except OSError as exc:
            print(f"  skipped {path}: {exc}", file=sys.stderr)
            continue
        state.finalize_pending_result()
        state.archive.extend(state.combat_history)
        combats.extend(c for c in state.archive
                       if sum(1 for t in c.trace if t[0] == "attack") >= args.min_swings)
    if args.limit:
        combats = combats[:args.limit]
    print(f"\ncombats with a recorded move list: {len(combats)}\n", flush=True)

    outcome: Counter = Counter()
    missing: Counter = Counter()
    findings: list[tuple] = []
    followed: list[float] = []
    for c in combats:
        replay = ScriptedCombat(board_from_snapshot(c.my_board, powers, trinkets),
                                board_from_snapshot(c.opponent_board, powers, trinkets),
                                c.trace, damage_cap=c.damage_cap or 0)
        total = len(replay.script)
        try:
            done = replay.run_scripted()
        except Divergence as div:
            outcome[div.kind] += 1
            findings.append((c, div.kind, div.step, total, div.detail))
            followed.append(div.step / max(1, total))
        except Stop as stop:
            # The last swing of a won fight is aimed at the losing hero, which is
            # not a minion and not a divergence: the board fight is over.
            if stop.step >= total - 1:
                outcome["clean"] += 1
                followed.append(1.0)
            else:
                outcome["untrackable"] += 1
                findings.append((c, "stop", stop.step, total, stop.reason))
                followed.append(stop.step / max(1, total))
                if replay.missing_card:
                    missing[replay.missing_card] += 1
        except (RecursionError, ValueError, IndexError, KeyError, TypeError) as exc:
            outcome["crash"] += 1
            findings.append((c, "crash", replay.step, total, repr(exc)))
        else:
            outcome["clean"] += 1
            followed.append(done / max(1, total))

    n = max(1, len(combats))
    print("how far the simulator followed the real fight:")
    for key in ("clean", "order", "state", "untrackable", "crash"):
        if outcome[key]:
            print(f"  {key:12s} {outcome[key]:4d}  = {100*outcome[key]/n:5.1f}%")
    if followed:
        followed.sort()
        print(f"  median share of swings replayed before stopping: "
              f"{100*followed[len(followed)//2]:.0f}%")

    print("\nengine/game disagreements, earliest first:")
    real = [f for f in findings if f[1] in ("order", "state", "crash")]
    real.sort(key=lambda f: f[2])
    for c, kind, step, total, detail in real[:args.show]:
        print(f"  turn {c.turn:2d} vs p{c.opponent_player_id} — {kind} at swing "
              f"{step + 1}/{total} (actual {c.actual_result})")
        print(f"      {detail}")
    if not real:
        print("  none")

    if missing:
        print("\nbodies the game summoned that the simulator never produced —\n"
              "the ranked list of what to model next:")
        for card_id, count in missing.most_common(20):
            card = db.en_by_id.get(card_id) or db.en_by_id.get(card_id.rstrip("t")) or {}
            text = _clean(card.get("text", ""))[:64]
            print(f"  {count:3d}  {card_id:22s} {card.get('name', '?'):26s} {text}")

    print("\nwhy the trackable region ended, when it did:")
    import re as _re
    reasons = Counter(_re.sub(r"\b\d+\b", "N", detail).split("(")[0].strip()[:70]
                      for _, kind, _, _, detail in findings if kind == "stop")
    for reason, count in reasons.most_common(8):
        print(f"  {count:4d}  {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
