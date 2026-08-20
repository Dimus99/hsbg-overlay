"""Monte-Carlo driver: repeat a randomised combat and summarise the spread.

Combats are independent, so the work parallelises cleanly. A persistent process
pool is kept alive between predictions — spawning workers costs far more than
the simulation itself.
"""
from __future__ import annotations

import math
import os
import random
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

from . import effects as effects_module
from .engine import Combat
from .model import SimBoard, board_from_snapshot

if TYPE_CHECKING:
    from ..gamestate import BoardSnapshot


@dataclass
class SimResult:
    iterations: int = 0
    wins: int = 0
    ties: int = 0
    losses: int = 0
    damage_dealt: list[int] = field(default_factory=list)
    damage_taken: list[int] = field(default_factory=list)
    elapsed: float = 0.0
    unknown_cards: tuple[str, ...] = ()
    coverage: float = 1.0

    # -- derived ---------------------------------------------------------

    @property
    def total(self) -> int:
        return max(1, self.wins + self.ties + self.losses)

    @property
    def win_pct(self) -> float:
        return 100.0 * self.wins / self.total

    @property
    def tie_pct(self) -> float:
        return 100.0 * self.ties / self.total

    @property
    def loss_pct(self) -> float:
        return 100.0 * self.losses / self.total

    @property
    def avg_damage_dealt(self) -> float:
        return sum(self.damage_dealt) / len(self.damage_dealt) if self.damage_dealt else 0.0

    @property
    def avg_damage_taken(self) -> float:
        return sum(self.damage_taken) / len(self.damage_taken) if self.damage_taken else 0.0

    @property
    def max_damage_taken(self) -> int:
        return max(self.damage_taken) if self.damage_taken else 0

    def lethal_risk(self, my_effective_health: int) -> float:
        """Share of simulated combats that would kill us outright."""
        if my_effective_health <= 0 or not self.damage_taken:
            return 0.0
        deadly = sum(1 for d in self.damage_taken if d >= my_effective_health)
        return 100.0 * deadly / self.total

    @property
    def margin_of_error(self) -> float:
        """95% confidence half-width on the win rate, in percentage points."""
        n = self.total
        p = self.wins / n
        return 100.0 * 1.96 * math.sqrt(max(p * (1 - p), 1e-9) / n)

    def merge(self, other: "SimResult") -> "SimResult":
        self.iterations += other.iterations
        self.wins += other.wins
        self.ties += other.ties
        self.losses += other.losses
        self.damage_dealt.extend(other.damage_dealt)
        self.damage_taken.extend(other.damage_taken)
        return self


# --------------------------------------------------------------------------
# worker side
# --------------------------------------------------------------------------

_WORKER_READY = False


def _exit_with_parent(interval: float = 2.0) -> None:
    """Leave when the overlay does.

    ``ProcessPoolExecutor`` only tears its workers down through an atexit hook,
    which a SIGTERM never reaches — and the launcher's Stop button *is* a
    SIGTERM. Without this the workers become orphans that outlive the overlay,
    a whole CPU's worth of idle processes each time. Watching for the reparent
    to launchd (ppid 1) costs one sleeping thread and needs no cooperation from
    the dying parent.
    """
    parent = os.getppid()

    def watch() -> None:
        while os.getppid() == parent:
            time.sleep(interval)
        os._exit(0)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()


def _init_worker(spec: dict[str, dict[str, Any]],
                 pool: Optional[list[dict[str, Any]]] = None) -> None:
    global _WORKER_READY
    _exit_with_parent()
    effects_module.configure(spec, pool)
    _WORKER_READY = True


def _run_batch(args) -> tuple[int, int, int, list[int], list[int]]:
    board_a, board_b, iterations, seed, damage_cap = args
    rng = random.Random(seed)
    wins = ties = losses = 0
    dealt: list[int] = []
    taken: list[int] = []
    for _ in range(iterations):
        outcome = Combat(board_a.clone(), board_b.clone(), rng,
                         effects=effects_module, damage_cap=damage_cap).run()
        if outcome.result == "win":
            wins += 1
            dealt.append(outcome.damage)
        elif outcome.result == "loss":
            losses += 1
            taken.append(outcome.damage)
        else:
            ties += 1
    return wins, ties, losses, dealt, taken


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

class Simulator:
    """Owns the worker pool and the derived effect table."""

    def __init__(self, effect_spec: Optional[dict[str, dict[str, Any]]] = None,
                 workers: int = 0, token_pool: Optional[list[dict[str, Any]]] = None):
        self.effect_spec = effect_spec or {}
        self.token_pool = token_pool or []
        self.workers = workers or max(1, (os.cpu_count() or 4) - 1)
        self._pool: Optional[ProcessPoolExecutor] = None
        effects_module.configure(self.effect_spec, self.token_pool)

    def _ensure_pool(self) -> Optional[ProcessPoolExecutor]:
        if self.workers <= 1:
            return None
        if self._pool is None:
            self._pool = ProcessPoolExecutor(
                max_workers=self.workers,
                initializer=_init_worker,
                initargs=(self.effect_spec, self.token_pool),
            )
        return self._pool

    def shutdown(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    # ------------------------------------------------------------------

    def run(self, board_a: SimBoard, board_b: SimBoard, iterations: int = 2000,
            seed: Optional[int] = None, damage_cap: int = 0,
            time_budget: float = 0.0) -> SimResult:
        started = time.perf_counter()
        seed = seed if seed is not None else random.randrange(1 << 30)
        result = SimResult()

        pool = self._ensure_pool()
        if pool is None:
            wins, ties, losses, dealt, taken = _run_batch(
                (board_a, board_b, iterations, seed, damage_cap))
            result.wins, result.ties, result.losses = wins, ties, losses
            result.damage_dealt, result.damage_taken = dealt, taken
            result.iterations = iterations
        else:
            chunks = self.workers
            per_chunk = max(1, iterations // chunks)
            jobs = [(board_a, board_b, per_chunk, seed + i, damage_cap)
                    for i in range(chunks)]
            try:
                timeout = time_budget if time_budget > 0 else None
                for wins, ties, losses, dealt, taken in pool.map(_run_batch, jobs,
                                                                 timeout=timeout):
                    result.wins += wins
                    result.ties += ties
                    result.losses += losses
                    result.damage_dealt.extend(dealt)
                    result.damage_taken.extend(taken)
                    result.iterations += per_chunk
            except Exception:
                # A dead pool must not take the overlay down with it: fall back
                # to running in-process and rebuild the pool next time.
                self.shutdown()
                wins, ties, losses, dealt, taken = _run_batch(
                    (board_a, board_b, min(iterations, 400), seed, damage_cap))
                result.wins, result.ties, result.losses = wins, ties, losses
                result.damage_dealt, result.damage_taken = dealt, taken
                result.iterations = min(iterations, 400)

        result.elapsed = time.perf_counter() - started
        return result


def coverage_of(board_a: SimBoard, board_b: SimBoard) -> tuple[float, tuple[str, ...]]:
    """Share of minions whose behaviour the simulator actually models.

    Minions with no special text count as covered — their printed stats are the
    whole story. Only unmodelled deathrattles and triggers drag this down.
    """
    total = 0
    unknown: list[str] = []
    for board in (board_a, board_b):
        for m in board.minions:
            total += 1
            if effects_module.spec_for(m).get("unmodelled"):
                unknown.append(m.name or m.card_id)
    if total == 0:
        return 1.0, ()
    return 1.0 - len(unknown) / total, tuple(dict.fromkeys(unknown))


_DEFAULT: Optional[Simulator] = None


def default_simulator() -> Simulator:
    global _DEFAULT
    if _DEFAULT is None:
        from ..carddb import get_db
        db = get_db()
        _DEFAULT = Simulator(effect_spec=db.derive_effects() if db.loaded else {},
                             token_pool=db.derive_pool() if db.loaded else [])
    return _DEFAULT


def simulate_matchup(my_snapshot: "BoardSnapshot", opponent_snapshot: "BoardSnapshot",
                     iterations: int = 2000, simulator: Optional[Simulator] = None,
                     damage_cap: int = 0) -> SimResult:
    """Convenience wrapper used by the app and the replay tool."""
    sim = simulator or default_simulator()
    from ..carddb import get_db
    db = get_db()
    powers, trinkets = db.hero_power_specs, db.trinket_specs
    board_a = board_from_snapshot(my_snapshot, powers, trinkets)
    board_b = board_from_snapshot(opponent_snapshot, powers, trinkets)
    result = sim.run(board_a, board_b, iterations=iterations, damage_cap=damage_cap)
    result.coverage, result.unknown_cards = coverage_of(board_a, board_b)
    return result
