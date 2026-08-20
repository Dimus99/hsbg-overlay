"""Statistics shown in the overlay.

Two independent sources:

* **Your own history** — parsed straight out of the Hearthstone logs on this
  machine. Always available, never leaves the machine, and it is the only
  source that is actually about *you*.
* **An external tier list** — optional. The well-known Battlegrounds stats sites
  (HSReplay, Firestone) sit behind Cloudflare bot protection and have no public,
  unauthenticated API we can politely call, so nothing is configured by default.
  Point ``stats_url`` at any endpoint returning the documented JSON shape and it
  will be used and cached.

Expected external shape::

    {"heroes": [{"cardId": "TB_BaconShop_HERO_93", "name": "N'Zot",
                 "averagePlacement": 4.21, "games": 12045, "pickRate": 0.18}]}
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import CACHE_DIR, LOG_ROOTS
from .gamestate import BattlegroundsState
from .logfiles import read_log_file

STATS_CACHE = CACHE_DIR / "external-stats.json"
HISTORY_CACHE = CACHE_DIR / "history.json"


# --------------------------------------------------------------------------
# personal history
# --------------------------------------------------------------------------

@dataclass
class HeroRecord:
    hero_card_id: str = ""
    hero_name: str = ""
    games: int = 0
    placements: list[int] = field(default_factory=list)

    @property
    def average_placement(self) -> float:
        return sum(self.placements) / len(self.placements) if self.placements else 0.0

    @property
    def top4_rate(self) -> float:
        if not self.placements:
            return 0.0
        return 100.0 * sum(1 for p in self.placements if p <= 4) / len(self.placements)


@dataclass
class PersonalStats:
    games: int = 0
    placements: list[int] = field(default_factory=list)
    combat_wins: int = 0
    combat_ties: int = 0
    combat_losses: int = 0
    heroes: dict[str, HeroRecord] = field(default_factory=dict)
    scanned_logs: list[str] = field(default_factory=list)

    @property
    def average_placement(self) -> float:
        return sum(self.placements) / len(self.placements) if self.placements else 0.0

    @property
    def combat_win_rate(self) -> float:
        total = self.combat_wins + self.combat_ties + self.combat_losses
        return 100.0 * self.combat_wins / total if total else 0.0

    def record_for(self, hero_card_id: str) -> Optional[HeroRecord]:
        if not hero_card_id:
            return None
        return self.heroes.get(_base_hero(hero_card_id))


def _base_hero(card_id: str) -> str:
    """Skins share a hero: TB_BaconShop_HERO_36_SKIN_H -> TB_BaconShop_HERO_36."""
    for suffix in ("_SKIN_",):
        idx = card_id.find(suffix)
        if idx > 0:
            return card_id[:idx]
    return card_id


def _log_key(path: Path) -> str:
    """Identity of a log file's contents: it only ever grows."""
    try:
        info = path.stat()
    except OSError:
        return ""
    return f"{int(info.st_mtime)}-{info.st_size}"


# Share of one core the cold scan is allowed to use. It only runs for logs it
# has never seen, but the player may well be mid-match when it does.
SCAN_DUTY_CYCLE = 0.35
SCAN_CHUNK = 20_000


def _scan_one(path: Path) -> dict:
    """Parse one log into the handful of numbers the stats panel needs.

    Deliberately throttled: this runs on a background thread and the first scan
    of a fresh install walks every log on disk. Left unthrottled it pinned a
    core for the better part of a minute.
    """
    state = BattlegroundsState()
    mark = time.perf_counter()
    for i, line in enumerate(read_log_file(path)):
        state.feed_line(line)
        if i % SCAN_CHUNK == SCAN_CHUNK - 1:
            worked = time.perf_counter() - mark
            time.sleep(worked * (1.0 - SCAN_DUTY_CYCLE) / SCAN_DUTY_CYCLE)
            mark = time.perf_counter()
    state.finalize_pending_result()
    state.archive.extend(state.combat_history)

    result = {"wins": 0, "ties": 0, "losses": 0, "games": []}
    for record in state.archive:
        if record.actual_result == "win":
            result["wins"] += 1
        elif record.actual_result == "loss":
            result["losses"] += 1
        elif record.actual_result == "tie":
            result["ties"] += 1

    me = state.players.get(state.my_player_id)
    if me is not None and me.place:
        result["games"].append({"place": me.place,
                                "hero": _base_hero(me.hero_card_id),
                                "hero_name": me.hero_name})
    return result


def _load_cache() -> dict:
    try:
        return json.loads(HISTORY_CACHE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        HISTORY_CACHE.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
    except OSError:
        pass


def scan_history(paths: Optional[list[Path]] = None,
                 max_logs: int = 20) -> PersonalStats:
    """Build a personal record from the local logs, reusing past work.

    A finished log never changes, so its numbers are cached by size and mtime.
    Without that this walks close to a gigabyte of text on every launch — the
    first version burned a full core for 48 seconds doing exactly that.
    """
    if paths is None:
        paths = []
        for root in LOG_ROOTS:
            if root.is_dir():
                paths.extend(root.glob("Hearthstone_*/Power.log"))
        paths.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        paths = paths[:max_logs]

    cache = _load_cache()
    stats = PersonalStats()
    dirty = False

    for path in paths:
        key = _log_key(path)
        if not key:
            continue
        entry = cache.get(path.parent.name)
        if entry is None or entry.get("key") != key:
            try:
                data = _scan_one(path)
            except OSError:
                continue
            entry = {"key": key, **data}
            cache[path.parent.name] = entry
            dirty = True

        stats.scanned_logs.append(path.parent.name)
        stats.combat_wins += entry.get("wins", 0)
        stats.combat_ties += entry.get("ties", 0)
        stats.combat_losses += entry.get("losses", 0)
        for game in entry.get("games", []):
            place = int(game.get("place", 0))
            if not place:
                continue
            stats.games += 1
            stats.placements.append(place)
            hero = game.get("hero") or ""
            if hero:
                record = stats.heroes.setdefault(
                    hero, HeroRecord(hero_card_id=hero,
                                     hero_name=game.get("hero_name", "")))
                record.games += 1
                record.placements.append(place)
                if game.get("hero_name"):
                    record.hero_name = game["hero_name"]

    if dirty:
        _save_cache(cache)
    return stats


# --------------------------------------------------------------------------
# optional external tier list
# --------------------------------------------------------------------------

class ExternalStats:
    def __init__(self, url: str = "", ttl_hours: int = 24, offline: bool = False):
        self.url = url
        self.ttl = ttl_hours * 3600
        self.offline = offline
        self.heroes: dict[str, dict[str, Any]] = {}
        self.error = ""
        self.fetched_at = 0.0

    @property
    def available(self) -> bool:
        return bool(self.heroes)

    def load(self) -> bool:
        if STATS_CACHE.exists():
            try:
                payload = json.loads(STATS_CACHE.read_text("utf-8"))
                self.fetched_at = float(payload.get("_fetched_at", 0))
                if time.time() - self.fetched_at < self.ttl:
                    self._index(payload)
                    return True
            except (OSError, json.JSONDecodeError, ValueError):
                pass
        if not self.url or self.offline:
            self.error = "no external source configured"
            return False
        try:
            request = urllib.request.Request(
                self.url, headers={"User-Agent": "hsbg-overlay/1.0"})
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            self.error = str(exc)
            return False
        payload["_fetched_at"] = time.time()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        STATS_CACHE.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        self._index(payload)
        return True

    def _index(self, payload: dict[str, Any]) -> None:
        self.heroes = {}
        for entry in payload.get("heroes", []) or []:
            key = _base_hero(entry.get("cardId") or entry.get("id") or "")
            if key:
                self.heroes[key] = entry

    def hero(self, card_id: str) -> Optional[dict[str, Any]]:
        return self.heroes.get(_base_hero(card_id))
