#!/usr/bin/env python3
"""Score the simulator against what actually happened in recorded games.

    python3 tools/accuracy.py [logs...] [--iterations N]

For every combat in the logs it compares the predicted distribution with the
real outcome, and reports how often the most likely prediction was right plus
the Brier score of the win probability (lower is better; 0.25 = coin flip).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hsbg.config import LOG_ROOTS                       # noqa: E402
from hsbg.gamestate import BattlegroundsState           # noqa: E402
from hsbg.logfiles import read_log_file                 # noqa: E402
from hsbg.sim.runner import simulate_matchup, default_simulator  # noqa: E402


def all_logs() -> list[Path]:
    out = []
    for root in LOG_ROOTS:
        if root.is_dir():
            out.extend(sorted(root.glob("Hearthstone_*/Power.log")))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="*")
    ap.add_argument("--iterations", type=int, default=1500)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    paths = [Path(p) for p in args.logs] or all_logs()
    if not paths:
        print("No logs found.", file=sys.stderr)
        return 1

    sim = default_simulator()
    combats = []
    for path in paths:
        print(f"  parsing {path.parent.name}", flush=True)
        state = BattlegroundsState()
        try:
            state.feed_lines(read_log_file(path))
        except OSError as exc:
            print(f"  skipped {path}: {exc}", file=sys.stderr)
            continue
        state.finalize_pending_result()
        state.archive.extend(state.combat_history)
        combats.extend(c for c in state.archive if c.actual_result)

    print(f"logs: {len(paths)}   combats with a known outcome: {len(combats)}", flush=True)
    if not combats:
        return 0

    correct = 0
    brier = 0.0
    damage_error = []
    coverage_sum = 0.0
    scored = 0
    # Split the score by whether every card on the two boards is modelled.
    buckets = {"full": [0, 0, 0.0], "partial": [0, 0, 0.0]}

    for c in combats:
        if not c.my_board.minions and not c.opponent_board.minions:
            continue
        result = simulate_matchup(c.my_board, c.opponent_board,
                                  iterations=args.iterations, simulator=sim,
                                  damage_cap=c.damage_cap)
        scored += 1
        if scored % 25 == 0:
            print(f"  ... {scored} combats scored", flush=True)
        coverage_sum += result.coverage
        predicted = max((("win", result.win_pct), ("tie", result.tie_pct),
                         ("loss", result.loss_pct)), key=lambda kv: kv[1])[0]
        hit = predicted == c.actual_result
        correct += hit
        actual_win = 1.0 if c.actual_result == "win" else 0.0
        brier += (result.win_pct / 100.0 - actual_win) ** 2
        bucket = buckets["full" if result.coverage > 0.999 else "partial"]
        bucket[0] += hit
        bucket[1] += 1
        bucket[2] += (result.win_pct / 100.0 - actual_win) ** 2
        if c.actual_result == "win":
            damage_error.append(abs(result.avg_damage_dealt - c.actual_damage))
        elif c.actual_result == "loss":
            damage_error.append(abs(result.avg_damage_taken - c.actual_damage))

        if args.verbose or not hit:
            mark = "ok " if hit else "MISS"
            print(f"  {mark} turn {c.turn:>2} vs p{c.opponent_player_id}: "
                  f"predicted {predicted:<4} (W{result.win_pct:.0f}/T{result.tie_pct:.0f}/"
                  f"L{result.loss_pct:.0f})  actual {c.actual_result} "
                  f"({c.actual_damage} dmg)  coverage {result.coverage:.0%}"
                  + (f"  unmodelled: {', '.join(result.unknown_cards[:3])}"
                     if result.unknown_cards else ""))

    if not scored:
        return 0
    print(f"\nmost-likely outcome correct : {correct}/{scored} = {100 * correct / scored:.1f}%")
    print(f"Brier score (win prob)      : {brier / scored:.4f}   (0.25 = coin flip, lower better)")
    if damage_error:
        print(f"mean damage error           : {sum(damage_error) / len(damage_error):.1f}")
    print(f"mean card coverage          : {coverage_sum / scored:.0%}")
    for label, (hits, n, b) in buckets.items():
        if n:
            print(f"  {label:<8} coverage: {hits}/{n} = {100 * hits / n:.1f}%  "
                  f"Brier {b / n:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
