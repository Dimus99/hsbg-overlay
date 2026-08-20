#!/usr/bin/env python3
"""Show the overlay against a recorded game — no live Hearthstone needed.

    python3 tools/preview.py [log] [--combat N]

Replays the log up to the Nth combat, then renders the panel exactly as it
would appear in a real match. Useful for checking layout and wording.
"""
from __future__ import annotations

import argparse
import multiprocessing
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    from PyObjCTools import AppHelper

    from hsbg.app import App
    from hsbg.config import Settings
    from hsbg.logfiles import newest_log_dir, read_log_file
    from hsbg.ui.overlay import OverlayController

    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?")
    ap.add_argument("--combat", type=int, default=6, help="stop after this many combats")
    ap.add_argument("--shop", action="store_true",
                    help="stop in the shop phase after that combat (prediction mode)")
    args = ap.parse_args()

    path = Path(args.log) if args.log else (newest_log_dir() or Path(".")) / "Power.log"
    if not path.exists():
        print(f"no such log: {path}", file=sys.stderr)
        return 1

    settings = Settings.load()
    settings.show_when_hs_inactive = True     # so the panels show while we look

    overlay = OverlayController(settings)
    app = App(settings, overlay=overlay)
    app.prepare()
    overlay.build()

    seen = 0
    stop_after = args.combat
    reached = False
    for line in read_log_file(path):
        app.state.feed_line(line)
        if len(app.state.combat_history) > seen:
            seen = len(app.state.combat_history)
            if seen >= stop_after:
                reached = True
                if not args.shop:
                    break
        if reached and args.shop and app.state.phase == "shop":
            break

    print(f"replayed to combat {seen}, phase={app.state.phase}, turn={app.state.turn}")
    app._recompute()
    print("overlay is on screen — Ctrl+C in this terminal to close")
    AppHelper.runEventLoop()
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
