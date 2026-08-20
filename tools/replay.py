#!/usr/bin/env python3
"""Replay a finished Power.log through the parser — the offline test harness.

    python3 tools/replay.py /path/to/Power.log [--combats] [--sim]

Without a path it picks the newest Hearthstone log folder.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hsbg.gamestate import BattlegroundsState, BoardSnapshot  # noqa: E402
from hsbg.logfiles import newest_log_dir, read_log_file       # noqa: E402


def fmt_minion(m) -> str:
    kw = "".join([
        "T" if m.taunt else "", "D" if m.divine_shield else "",
        "P" if m.poisonous else "", "V" if m.venomous else "",
        "R" if m.reborn else "", "W" if m.mega_windfury else ("w" if m.windfury else ""),
    ])
    gold = "*" if m.golden else " "
    return f"{m.position}:{gold}{m.name or m.card_id}[{m.card_id}] {m.attack}/{m.health} t{m.tier} {kw}"


def dump_board(label: str, b: BoardSnapshot) -> None:
    print(f"  {label}: {b.player_name or '?'} ({b.hero_name or b.hero_card_id}) "
          f"hp={b.hero_health}-{b.hero_armor}a tier={b.tech_level}")
    if not b.minions:
        print("      <empty>")
    for m in b.minions:
        print("      " + fmt_minion(m))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?")
    ap.add_argument("--combats", action="store_true", help="print every combat's boards")
    ap.add_argument("--sim", action="store_true", help="also run the simulator on each combat")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--iterations", type=int, default=1500)
    args = ap.parse_args()

    if args.log:
        path = Path(args.log)
    else:
        d = newest_log_dir()
        if d is None:
            print("No Hearthstone logs found.", file=sys.stderr)
            return 1
        path = d / "Power.log"
    print(f"Replaying {path}")

    state = BattlegroundsState()
    state.feed_lines(read_log_file(path))

    print(f"\ngame_type={state.parser.game_type} me={state.my_player_id} "
          f"ghost={state.ghost_player_id} turn={state.turn}")
    print(f"players known: {len(state.players)}  combats: {len(state.combat_history)}")

    print("\n--- leaderboard ---")
    for p in state.leaderboard():
        print(f"  #{p.place or '?':<3} {p.name or '?':<16} {p.hero_name or p.hero_card_id:<22} "
              f"hp={max(0, p.health - p.damage)}+{p.armor} tier={p.tech_level}")

    if args.combats:
        combats = state.combat_history
        if args.limit:
            combats = combats[-args.limit:]
        for i, c in enumerate(combats, 1):
            print(f"\n--- combat #{i} turn {c.turn} vs player {c.opponent_player_id} ---")
            dump_board("me ", c.my_board)
            dump_board("opp", c.opponent_board)
            if args.sim:
                from hsbg.sim.runner import simulate_matchup
                r = simulate_matchup(c.my_board, c.opponent_board, iterations=args.iterations,
                                     damage_cap=c.damage_cap)
                health = max(0, c.my_board.hero_health) + c.my_board.hero_armor
                print(f"      => win {r.win_pct:.1f}%  tie {r.tie_pct:.1f}%  loss {r.loss_pct:.1f}%"
                      f"  avg dmg dealt {r.avg_damage_dealt:.1f} taken {r.avg_damage_taken:.1f}"
                      f"  lethal risk {r.lethal_risk(health):.1f}%"
                      f"  coverage {r.coverage:.0%}")
                if c.actual_result:
                    print(f"         actual: {c.actual_result} ({c.actual_damage} dmg)")

    print("\n--- minions seen (pool) ---")
    top = sorted(state.seen_cards.items(), key=lambda kv: -kv[1])[:15]
    for card, n in top:
        print(f"  {card:<18} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
