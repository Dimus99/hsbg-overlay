#!/usr/bin/env python3
"""Read the debug journal and rank the fights the simulator called wrong.

    python3 tools/debugreview.py [--dir PATH] [--show N] [--min-surprise P]
                                 [--boards] [--json]

The journal (switched on from the menu bar: "Debug: combat journal") writes one
line per finished fight — the prediction beside what actually happened, plus
both boards in full. This reads it back, worst call first.

"Worst" means most confidently wrong, not most often wrong. A fight called at
50/50 that goes the other way is nothing; a fight called at 0% that is *won* is
a bug with a name, because 2000 randomised runs found no path to a win at all —
so the board being simulated was not the board being played. Those come first,
and ``--boards`` prints both warbands so the culprit can be found by eye.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hsbg.carddb import get_db                              # noqa: E402
from hsbg.config import DEBUG_DIR                           # noqa: E402
from hsbg.debuglog import SURPRISE_THRESHOLD, read          # noqa: E402

def _kw(m: dict) -> str:
    flags = [c for c, key in (("T", "taunt"), ("D", "divine_shield"),
                              ("P", "poisonous"), ("V", "venomous"),
                              ("R", "reborn"), ("W", "windfury"),
                              ("S", "stealth")) if m.get(key)]
    return "".join(flags)


def _name(m: dict, db) -> str:
    name = m.get("name") or ""
    if db is not None and db.loaded:
        name = db.name(m.get("card", ""), fallback=name)
    return name or m.get("card", "?")


def _board_line(board: dict, db) -> str:
    if not board.get("minions"):
        return "      (empty)"
    out = []
    for m in board["minions"]:
        kw = _kw(m)
        out.append(f"      {m['atk']:>4}/{m['hp']:<4} {_name(m, db)}"
                   + (f" [{kw}]" if kw else ""))
    trinkets = board.get("trinkets") or []
    if trinkets:
        labels = [db.name(t, fallback=t) if db and db.loaded else t for t in trinkets]
        out.append(f"      trinkets: {', '.join(labels)}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(DEBUG_DIR))
    ap.add_argument("--show", type=int, default=15)
    ap.add_argument("--min-surprise", type=float, default=SURPRISE_THRESHOLD,
                    help="only fights where the prediction missed by this much")
    ap.add_argument("--boards", action="store_true", help="print both warbands")
    ap.add_argument("--json", action="store_true", help="dump matching rows as JSON")
    args = ap.parse_args()

    rows = read(Path(args.dir))
    if not rows:
        print(f"The journal is empty: {args.dir}\n"
              f'Switch on "Debug: combat journal" in the BG menu and play a few fights.',
              file=sys.stderr)
        return 1

    scored = [r for r in rows if r.get("prediction") and r.get("actual", {}).get("result")]
    print(f"fights in the journal: {len(rows)}   with a prediction: {len(scored)}")

    # Calibration, the only honest summary: of the fights called at N%, how many
    # actually went that way?
    buckets = ((0, 2), (2, 20), (20, 40), (40, 60), (60, 80), (80, 98), (98, 101))
    print("\ncalibration of the win prediction:")
    for lo, hi in buckets:
        group = [r for r in scored if lo <= r["prediction"]["win"] < hi]
        if not group:
            continue
        predicted = sum(r["prediction"]["win"] for r in group) / len(group)
        actual = 100.0 * sum(1 for r in group
                             if r["actual"]["result"] == "win") / len(group)
        mark = "  ← off" if abs(predicted - actual) > 12 else ""
        print(f"  {lo:3d}-{hi:3d}%  n={len(group):3d}  promised {predicted:5.1f}%  "
              f"actual {actual:5.1f}%{mark}")

    misses = [r for r in scored if r.get("surprise", 0.0) >= args.min_surprise]
    misses.sort(key=lambda r: -r.get("surprise", 0.0))

    if args.json:
        print(json.dumps(misses[:args.show], ensure_ascii=False, indent=2))
        return 0

    print(f"\nmisses with a margin ≥{args.min_surprise:g}: {len(misses)}")
    kinds = Counter(f"{r['prediction'] and _called(r)} → {r['actual']['result']}"
                    for r in misses)
    for kind, count in kinds.most_common():
        print(f"  {count:3d}  {kind}")

    db = get_db(offline=True)
    print("\nworst calls — confident and wrong:")
    for r in misses[:args.show]:
        p, a = r["prediction"], r["actual"]
        flag = "" if a.get("from_log") else "  (outcome inferred from health, not from the log)"
        unknown = p.get("unknown_cards") or []
        print(f"\n  turn {r['turn']:>2} against p{r['opponent_player_id']} — "
              f"promised W{p['win']:.0f}/T{p['tie']:.0f}/L{p['loss']:.0f}, "
              f"actual {a['result']} "
              f"({a['damage']} damage, {a.get('attacks', 0)} swings){flag}")
        print(f"      coverage {p['coverage']:.0%}"
              + (f", not modelled: {', '.join(unknown)}" if unknown else "")
              + f", runs {p['iterations']}")
        if args.boards:
            print("    my board:")
            print(_board_line(r["my_board"], db))
            print("    opponent board:")
            print(_board_line(r["opponent_board"], db))
    return 0


def _called(row: dict) -> str:
    p = row["prediction"]
    best = max(("win", p["win"]), ("tie", p["tie"]), ("loss", p["loss"]),
               key=lambda kv: kv[1])
    return best[0]


if __name__ == "__main__":
    raise SystemExit(main())
