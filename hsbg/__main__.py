"""Entry point: ``python -m hsbg``."""
from __future__ import annotations

import argparse
import multiprocessing
import sys

# True inside the built .app, where there is no interpreter to pass "-m hsbg"
# to and no terminal to print to.
FROZEN = getattr(sys, "frozen", False)


# Widest label in either language, so both columns line up.
LABEL_WIDTH = 19


def _diagnostics() -> int:
    from .carddb import CardDB
    from .config import Settings, hearthstone_is_fullscreen, detect_game_language
    from .i18n import strings
    from .logfiles import ensure_log_config, newest_log_dir

    settings = Settings.load()
    t = strings(settings.resolved_language())

    def row(label_key: str, value) -> None:
        print(f"  {t(label_key).ljust(LABEL_WIDTH)}: {value}")

    print(t("check.header"))
    log_dir = newest_log_dir()
    row("check.log_dir", log_dir or t("check.not_found"))
    changed, message = ensure_log_config()
    row("check.log_config", t(message))
    row("check.language", t("check.setting", value=detect_game_language(),
                            setting=settings.language))
    fullscreen = hearthstone_is_fullscreen()
    row("check.fullscreen", t("check.fullscreen_yes") if fullscreen else t("check.no"))

    db = CardDB(locale=settings.resolved_language(), offline=settings.offline)
    if db.load():
        spec = db.derive_effects()
        row("check.card_db", t("check.cards", cards=len(db.by_id), effects=len(spec)))
    else:
        row("check.card_db", t("check.db_error", error=db.error))

    try:
        from .ui.hswindow import find_hearthstone_window, hearthstone_is_running
        row("check.hs_running", t("check.yes") if hearthstone_is_running()
                                else t("check.no"))
        rect = find_hearthstone_window()
        row("check.hs_window", rect if rect else t("check.window_missing"))
    except ImportError as exc:
        row("check.appkit", t("check.appkit_missing", error=exc))
    return 0


def _headless() -> int:
    """Run the pipeline without a window and print the panel as text."""
    import time
    from .app import App
    from .config import Settings

    settings = Settings.load()
    app = App(settings, overlay=None)
    t = app.t
    app.prepare()
    app.start()
    print(t("headless.banner"))
    try:
        while True:
            time.sleep(2.0)
            model = getattr(app, "last_model", None)
            if model is None:
                continue
            print("\n" + t("headless.line", status=model.status, turn=model.turn,
                             health=model.my_health, tier=model.my_tier))
            if model.odds_pending:
                print("  " + t("headless.pending"))
            elif model.odds:
                o = model.odds
                print("  " + t("headless.odds", headline=o.headline,
                               win=f"{o.win:.0f}", tie=f"{o.tie:.0f}",
                               loss=f"{o.loss:.0f}",
                               damage=f"{o.avg_damage_taken:.1f}"))
            for opp in model.opponents:
                names = ", ".join(f"{m.name} {m.attack}/{m.health}" for m in opp.minions)
                print(f"  {opp.name or opp.hero}: {names or t('headless.no_board')}")
    except KeyboardInterrupt:
        app.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    from . import __version__
    from .config import Settings
    from .i18n import strings

    t = strings(Settings.load().resolved_language())
    parser = argparse.ArgumentParser(prog="hsbg", description=t("cli.description"))
    parser.add_argument("--check", action="store_true", help=t("cli.check"))
    parser.add_argument("--headless", action="store_true", help=t("cli.headless"))
    parser.add_argument("--launcher", action="store_true", help=t("cli.launcher"))
    parser.add_argument("--overlay", action="store_true", help=t("cli.overlay"))
    parser.add_argument("--iterations", type=int, help=t("cli.iterations"))
    parser.add_argument("--version", action="version",
                        version=f"HSBG Overlay {__version__}",
                        help=t("cli.version"))
    args, _unknown = parser.parse_known_args(argv)

    if args.check:
        return _diagnostics()
    if args.headless:
        return _headless()
    # Double-clicking the .app should open the control panel, not a process
    # with no window; from a terminal the overlay itself stays the default.
    if args.launcher or (FROZEN and not args.overlay):
        from .launcher import main as launcher_main
        return launcher_main()

    from PyObjCTools import AppHelper
    from .app import App
    from .ui.overlay import OverlayController

    settings = Settings.load()
    if args.iterations:
        settings.iterations = args.iterations

    overlay = OverlayController(settings)
    app = App(settings, overlay=overlay)
    overlay.on_quit = app.stop
    overlay.recompute_hook = app._wake.set
    overlay.debug_log_hook = app.set_debug_log
    overlay.resolve_hover = app.resolve_hover

    app.prepare()
    overlay.build()
    app.start()
    AppHelper.runEventLoop()
    return 0


if __name__ == "__main__":
    # 'spawn' is the macOS default; the guard keeps worker processes from
    # re-running the app when they re-import this module.
    multiprocessing.freeze_support()
    sys.exit(main())
