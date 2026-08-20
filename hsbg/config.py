"""User-tunable settings and well-known filesystem locations."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

APP_NAME = "hsbg-overlay"

HOME = Path.home()
SUPPORT_DIR = HOME / "Library" / "Application Support" / APP_NAME
CACHE_DIR = SUPPORT_DIR / "cache"
DEBUG_DIR = SUPPORT_DIR / "debug"
CONFIG_PATH = SUPPORT_DIR / "settings.json"

# Where Hearthstone keeps its rotating per-launch log folders. The Battle.net
# installer uses the first one; the second shows up on some setups.
LOG_ROOTS = [
    Path("/Applications/Hearthstone/Logs"),
    HOME / "Library" / "Logs" / "Hearthstone",
]

# log.config lives outside the app bundle so it survives patches.
LOG_CONFIG_PATH = HOME / "Library" / "Preferences" / "Blizzard" / "Hearthstone" / "log.config"
HS_OPTIONS_PATH = HOME / "Library" / "Preferences" / "Blizzard" / "Hearthstone" / "options.txt"

# Log sections we need. [Power] with Verbose is what carries the board state.
REQUIRED_LOG_SECTIONS = {
    "Power": {"LogLevel": "1", "FilePrinting": "true", "ConsolePrinting": "false",
              "ScreenPrinting": "false", "Verbose": "true"},
    "LoadingScreen": {"LogLevel": "1", "FilePrinting": "true", "ConsolePrinting": "false",
                      "ScreenPrinting": "false"},
}


@dataclass
class Settings:
    # --- simulation ---
    iterations: int = 2000          # Monte-Carlo runs per prediction
    sim_workers: int = 0            # 0 = auto (cpu_count - 1)
    sim_time_budget: float = 2.5    # seconds; run fewer iterations rather than overrun
    # How long a finished fight stays on screen if you never click anything.
    # Normally the odds disappear on your first tavern action instead.
    combat_hold_seconds: float = 60.0

    # --- interface ---
    language: str = "auto"          # auto | ruRU | enUS
    overlay_scale: float = 1.0
    overlay_opacity: float = 0.88
    anchor: str = "top-left"        # corner of the Hearthstone window to pin to
    offset_x: int = 16
    offset_y: int = 16
    show_when_hs_inactive: bool = False
    # "?" pins drawn on Hearthstone's own opponent rail, marking the portraits
    # whose board pops up on hover. Off by default — they sit on top of the
    # game's own art, so they are opt-in, from the switch in the lobby table's
    # header or the menu bar item.
    show_hover_marks: bool = False

    # --- panels ---
    show_opponents: bool = True
    show_leaderboard: bool = True
    show_pool: bool = True
    show_stats: bool = True

    # --- data ---
    offline: bool = False
    stats_refresh_hours: int = 24

    # --- debug ---
    log_unknown_cards: bool = True
    verbose: bool = False
    # Write one line per finished fight — the prediction beside what actually
    # happened — to DEBUG_DIR. Off by default: it is a diagnostic for chasing
    # simulator bugs, not something a normal session needs. Toggled from the
    # menu bar item.
    debug_log: bool = False

    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Settings":
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
            known = {f for f in cls.__dataclass_fields__}
            return cls(**{k: v for k, v in raw.items() if k in known})
        s = cls()
        s.save()
        return s

    def save(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), "utf-8")

    def resolved_language(self) -> str:
        if self.language != "auto":
            return self.language
        return detect_game_language()


# Locales HearthstoneJSON publishes; the game records one of these.
KNOWN_LOCALES = ("ruRU", "enUS", "enGB", "deDE", "frFR", "esES", "esMX", "itIT",
                 "ptBR", "plPL", "koKR", "zhCN", "zhTW", "jaJP", "thTH")

PRODUCT_DB_PATHS = [Path("/Applications/Hearthstone/.product.db")]
BATTLE_NET_CONFIG = (HOME / "Library" / "Application Support" / "Battle.net"
                     / "Battle.net.config")

# Third line of every Hearthstone.log: "I 01:30:43.594 SetLocale: enUS".
RE_SET_LOCALE = re.compile(r"SetLocale:\s*([A-Za-z]{4})")
# The line is written during startup; no need to read the whole log to find it.
LOCALE_SCAN_LINES = 60


def _known(locale: str) -> str:
    """A locale we can actually use, or "" — enGB shares enUS card data."""
    if locale == "enGB":
        return "enUS"
    return locale if locale in KNOWN_LOCALES else ""


def _locale_from_logs() -> str:
    """The language the game itself last started in.

    Hearthstone announces it at the top of ``Hearthstone.log``, which makes this
    the only source that follows a language switch the moment the game is
    restarted — and the only one that speaks for the *game* rather than for
    Battle.net.
    """
    newest: Optional[Path] = None
    newest_at = -1.0
    for root in LOG_ROOTS:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            log = child / "Hearthstone.log"
            try:
                stamp = log.stat().st_mtime
            except OSError:
                continue
            if stamp > newest_at:
                newest, newest_at = log, stamp
    if newest is None:
        return ""
    try:
        with newest.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(LOCALE_SCAN_LINES):
                line = handle.readline()
                if not line:
                    break
                match = RE_SET_LOCALE.search(line)
                if match:
                    return _known(match.group(1))
    except OSError:
        pass
    return ""


def _varint(blob: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while i < len(blob):
        byte = blob[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7
    return -1, i


def _protobuf_fields(blob: bytes):
    """Yield ``(field number, wire type, value)`` for one nesting level."""
    i = 0
    while i < len(blob):
        key, i = _varint(blob, i)
        if key < 0:
            return
        field_no, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(blob, i)
        elif wire == 2:
            length, i = _varint(blob, i)
            if length < 0 or i + length > len(blob):
                return
            value, i = blob[i:i + length], i + length
        elif wire == 5:
            value, i = blob[i:i + 4], i + 4
        elif wire == 1:
            value, i = blob[i:i + 8], i + 8
        else:
            return
        yield field_no, wire, value


# Battle.net's product record is a protobuf: field 3 is the install's settings,
# and field 6 inside it is the selected *text* language.
PB_SETTINGS = 3
PB_TEXT_LANGUAGE = 6


def _locale_from_product_db() -> str:
    """The language Battle.net has Hearthstone set to.

    Read by field number rather than by scanning the file for any locale string:
    the settings are followed by the list of *installed* languages, so a client
    with both Russian and English on disk always came out as whichever we
    happened to check for first — Russian — no matter what the game showed.
    """
    for path in PRODUCT_DB_PATHS:
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        for field_no, wire, value in _protobuf_fields(blob):
            if field_no != PB_SETTINGS or wire != 2:
                continue
            for sub_no, sub_wire, sub_value in _protobuf_fields(value):
                if sub_no == PB_TEXT_LANGUAGE and sub_wire == 2:
                    locale = _known(sub_value.decode("ascii", "ignore"))
                    if locale:
                        return locale
    return ""


def _locale_from_battle_net() -> str:
    """Battle.net's *own* interface language — a guess, and only that.

    The launcher can be Russian while the game it starts is English, so this is
    a last resort, below anything that speaks for Hearthstone itself.
    """
    try:
        config = json.loads(BATTLE_NET_CONFIG.read_text("utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return ""
    for section in config.values():
        if isinstance(section, dict):
            client = section.get("Client")
            if isinstance(client, dict):
                locale = _known(str(client.get("Language", "")))
                if locale:
                    return locale
    return ""


def _locale_from_shell() -> str:
    lang = os.environ.get("LANG", "").lower()
    for locale in KNOWN_LOCALES:
        if lang.startswith(locale[:2].lower()):
            return _known(locale)
    return ""


def detect_game_language() -> str:
    """The language Hearthstone is running in — card names and our own labels
    both follow it, so that the overlay reads like part of the game.

    Sources in order of how much they know: what the game logged at startup,
    what Battle.net has it set to, what Battle.net itself is set to, the shell.
    """
    for source in (_locale_from_logs, _locale_from_product_db,
                   _locale_from_battle_net, _locale_from_shell):
        locale = source()
        if locale:
            return locale
    return "enUS"


def hearthstone_is_fullscreen() -> bool:
    """True exclusive fullscreen puts the game on its own Space, where no
    overlay window can be drawn. Worth warning the user about."""
    try:
        for line in HS_OPTIONS_PATH.read_text("utf-8", errors="replace").splitlines():
            key, _, value = line.partition("=")
            if key.strip().lower() == "graphicsfullscreen":
                return value.strip().lower() in ("true", "1")
    except OSError:
        pass
    return False
