"""Card database: names for the overlay, and combat effects derived from text.

Card data comes from HearthstoneJSON (a static, public, unauthenticated CDN).
We keep both the game's locale (for display) and English (for effect parsing,
because the English wording is what the patterns below are written against).
"""
from __future__ import annotations

import gzip
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import CACHE_DIR

CARDS_URL = "https://api.hearthstonejson.com/v1/latest/{locale}/cards.json"
USER_AGENT = "hsbg-overlay/1.0 (+local Battlegrounds overlay)"
CACHE_TTL_DAYS = 14

NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

RE_TAG = re.compile(r"<[^>]+>")
RE_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    text = RE_TAG.sub("", text or "")
    text = text.replace(" ", " ").replace("[x]", "")
    return RE_WS.sub(" ", text).strip()


def _num(word: str) -> int:
    word = word.strip().lower()
    if word.isdigit():
        return int(word)
    return NUMBER_WORDS.get(word, 1)


class CardDB:
    def __init__(self, locale: str = "enUS", offline: bool = False):
        self.locale = locale
        self.offline = offline
        self.by_id: dict[str, dict[str, Any]] = {}
        self.en_by_id: dict[str, dict[str, Any]] = {}
        self.loaded = False
        self.error: str = ""
        self._hero_powers: Optional[dict[str, dict[str, Any]]] = None
        self._trinkets: Optional[dict[str, dict[str, Any]]] = None

    # ------------------------------------------------------------ retrieval

    def _cache_path(self, locale: str) -> Path:
        return CACHE_DIR / f"cards-{locale}.json.gz"

    def _fetch(self, locale: str) -> list[dict[str, Any]]:
        path = self._cache_path(locale)
        fresh = path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL_DAYS * 86400
        if fresh or (self.offline and path.exists()):
            try:
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, json.JSONDecodeError):
                pass
        if self.offline:
            raise RuntimeError(f"no cached card data for {locale} and offline mode is on")

        request = urllib.request.Request(CARDS_URL.format(locale=locale),
                                         headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
        cards = json.loads(payload.decode("utf-8"))
        cards = [c for c in cards if _is_battlegrounds(c)]
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(cards, fh, ensure_ascii=False)
        return cards

    def load(self) -> bool:
        try:
            self.en_by_id = {c["id"]: c for c in self._fetch("enUS")}
            if self.locale == "enUS":
                self.by_id = self.en_by_id
            else:
                try:
                    self.by_id = {c["id"]: c for c in self._fetch(self.locale)}
                except (urllib.error.URLError, RuntimeError, OSError):
                    self.by_id = self.en_by_id
            self.loaded = True
            self.error = ""
        except (urllib.error.URLError, OSError, RuntimeError, json.JSONDecodeError) as exc:
            self.error = str(exc)
            self.loaded = False
        return self.loaded

    # -------------------------------------------------------------- lookups

    def card(self, card_id: str) -> dict[str, Any]:
        card = self.by_id.get(card_id)
        if card is None and card_id.endswith("_G"):
            card = self.by_id.get(card_id[:-2])
        return card or {}

    def name(self, card_id: str, fallback: str = "") -> str:
        card = self.card(card_id)
        name = card.get("name") or fallback or card_id
        if card_id.endswith("_G") and not card.get("id", "").endswith("_G"):
            name = f"{name} ★"
        return name

    def text(self, card_id: str) -> str:
        return _clean(self.card(card_id).get("text", ""))

    def tier(self, card_id: str) -> int:
        return int(self.card(card_id).get("techLevel", 0) or 0)

    def races(self, card_id: str) -> frozenset[str]:
        card = self.card(card_id)
        races = card.get("races") or ([card["race"]] if card.get("race") else [])
        return frozenset(races)

    def pool_minions(self, max_tier: int = 6) -> list[dict[str, Any]]:
        """Minions that can appear in Bob's tavern, for the pool tracker."""
        out = []
        for card in self.by_id.values():
            if card.get("type") != "MINION":
                continue
            tier = int(card.get("techLevel", 0) or 0)
            if not 1 <= tier <= max_tier:
                continue
            if card.get("id", "").endswith("_G"):
                continue
            if card.get("isBattlegroundsBuddy"):
                continue
            out.append(card)
        return out

    # -------------------------------------------------------------- effects

    def name_index(self) -> dict[str, dict[str, Any]]:
        """English minion name -> card, so 'Summon two Scallywags' can be
        resolved to the actual token's stats and keywords."""
        index: dict[str, dict[str, Any]] = {}
        for card in self.en_by_id.values():
            if card.get("type") != "MINION":
                continue
            index.setdefault((card.get("name") or "").lower(), card)
        return index

    def spell_index(self) -> dict[str, str]:
        """English spell name -> its text.

        A dozen minions read "Rally: Cast Queen's Command", and the spell they
        name is usually a plain buff. Inlining its text lets the ordinary
        payload parser handle them instead of needing a card list of its own.
        """
        index: dict[str, str] = {}
        for card in self.en_by_id.values():
            if card.get("type") not in ("BATTLEGROUND_SPELL", "SPELL"):
                continue
            name = (card.get("name") or "").strip().lower()
            text = _clean(card.get("text", ""))
            if name and text:
                index.setdefault(name, text)
        return index

    def derive_effects(self) -> dict[str, dict[str, Any]]:
        """Turn English card text into simulator effect specs."""
        index = self.name_index()
        spells = self.spell_index()
        spec: dict[str, dict[str, Any]] = {}
        for card_id, card in self.en_by_id.items():
            if card.get("type") != "MINION":
                continue
            parsed = parse_card_effects(_clean(card.get("text", "")),
                                        card.get("mechanics") or [], index, spells)
            if parsed:
                spec[card_id] = parsed
        return spec

    @property
    def hero_power_specs(self) -> dict[str, dict[str, Any]]:
        """Hero powers that keep working *inside* combat, by hero card id.

        Almost every hero power either acts in the tavern or fires at the start
        of combat, and both board snapshots are taken after the game has applied
        those — so they cost the prediction nothing. The exception is a power
        that changes bodies which appear *mid-fight*: no snapshot can show it,
        and it turns a symmetric-looking board into a loss. Greybough's "+1/+2
        and Taunt to minions you summon during combat" is the whole reason this
        exists — two identical 1/1 Boneheads traded, and his skeletons stood up
        as 2/3 Taunts while ours stayed 1/1.
        """
        if self._hero_powers is None:
            self._hero_powers = self._derive_hero_powers()
        return self._hero_powers

    def _derive_hero_powers(self) -> dict[str, dict[str, Any]]:
        by_dbf = {c.get("dbfId"): c for c in self.en_by_id.values()}
        out: dict[str, dict[str, Any]] = {}
        for card in self.en_by_id.values():
            if card.get("type") != "HERO":
                continue
            power = by_dbf.get(card.get("heroPowerDbfId"))
            if not power:
                continue
            spec = _hero_power_spec(_clean(power.get("text") or ""))
            if spec:
                out[card["id"]] = spec
        return out

    @property
    def trinket_specs(self) -> dict[str, dict[str, Any]]:
        """Trinket behaviour that still matters *inside* the fight, by card id.

        Deliberately a narrow projection of :func:`parse_card_effects`, not the
        whole thing: a trinket's Start-of-Combat buff is already in the snapshot's
        stats, so replaying it would count it twice. Only the parts that fire on
        an event which has not happened yet survive — an Avenge counter, and a
        body that appears the moment a slot opens.
        """
        if self._trinkets is None:
            index, spells = self.name_index(), self.spell_index()
            out: dict[str, dict[str, Any]] = {}
            for card in self.en_by_id.values():
                if card.get("type") != "BATTLEGROUND_TRINKET":
                    continue
                spec = _trinket_spec(_clean(card.get("text", "")), index, spells)
                if spec:
                    out[card["id"]] = spec
            self._trinkets = out
        return self._trinkets

    def derive_pool(self, max_tier: int = 6) -> list[dict[str, Any]]:
        """Flat token table behind "Summon a random Beast" and friends.

        Kept as plain dicts so it can be handed to worker processes alongside
        the effect spec.
        """
        keep = ("DEATHRATTLE", "DIVINE_SHIELD", "TAUNT", "BATTLECRY", "REBORN",
                "WINDFURY", "POISONOUS", "VENOMOUS", "STEALTH", "BACON_RALLY")
        cards = [c for c in self.en_by_id.values()
                 if c.get("type") == "MINION" and c.get("isBattlegroundsPoolMinion")
                 and 1 <= int(c.get("techLevel") or 0) <= max_tier
                 and not c.get("id", "").endswith("_G")]
        if len(cards) < 50:
            # Older card dumps do not carry the flag: fall back to every minion
            # that has a tavern tier, rotated-out ones included.
            cards = self.pool_minions(max_tier=max_tier)
        out: list[dict[str, Any]] = []
        for card in cards:
            mechanics = set(card.get("mechanics") or [])
            races = card.get("races") or ([card["race"]] if card.get("race") else [])
            out.append({
                "card_id": card.get("id", ""),
                "name": card.get("name", ""),
                "attack": int(card.get("attack") or 1),
                "health": int(card.get("health") or 1),
                "tier": int(card.get("techLevel") or 1),
                "races": tuple(races),
                "mechanics": tuple(m for m in keep if m in mechanics),
                "legendary": card.get("rarity") == "LEGENDARY",
                "taunt": "TAUNT" in mechanics,
                "divine_shield": "DIVINE_SHIELD" in mechanics,
                "poisonous": "POISONOUS" in mechanics,
                "venomous": "VENOMOUS" in mechanics,
                "windfury": "WINDFURY" in mechanics,
                "reborn": "REBORN" in mechanics,
                "stealth": "STEALTH" in mechanics,
            })
        return out


def _trinket_spec(text: str, index: dict[str, dict[str, Any]],
                  spells: dict[str, str]) -> dict[str, Any]:
    """The in-combat half of a trinket, or ``{}`` when it has none."""
    if not text:
        return {}
    text = _normalise(text, spells)
    spec: dict[str, Any] = {}

    match = RE_WHEN_SPACE.search(text)
    if match:
        clause = match.group(1)
        dead = RE_EXACT_COPY_DEAD.search(clause)
        if dead:
            # Boom Controller: the body comes back as it stood when it fell,
            # buffs and all, which is not the plain copy _summon_from_graveyard
            # normally makes.
            spec["when_space"] = {"summon": {"count": 1, "from_graveyard": {
                "race": _race_of(dead.group(1)), "exact": True}}}
        else:
            payload = _parse_payload(clause, index)
            payload.pop("buff", None)      # "+4/+4 to the minion in hand" is pre-fight
            if payload.get("summon") or payload.get("summon_from_hand"):
                spec["when_space"] = payload

    avenge = re.search(r"Avenge \((\d+)\)", text)
    if avenge:
        clause = _clause(text, "Avenge (")
        payload = _parse_payload(clause, index)
        gain = _parse_gain(clause)
        if gain:
            payload["gain"] = gain
        if payload:
            payload["threshold"] = int(avenge.group(1))
            spec["avenge"] = payload
    return spec


def _hero_power_spec(text: str) -> dict[str, Any]:
    """The part of a hero power that keeps acting *inside* the fight.

    Everything else a hero power does — tavern effects, start-of-combat buffs —
    is already baked into the snapshot's stats. These three are not: they fire
    on events that have not happened yet when the snapshot is taken.
    """
    spec: dict[str, Any] = {}

    match = RE_SUMMON_BUFF.search(text)
    if match:
        buff: dict[str, Any] = {"attack": int(match.group(1)),
                                "health": int(match.group(2))}
        keyword = (match.group(3) or "").lower()
        if keyword:
            buff[{"taunt": "taunt", "divine shield": "divine_shield",
                  "windfury": "windfury"}[keyword]] = True
        spec["summon_buff"] = buff

    match = RE_HERO_KILL_BUFF.search(text)
    if match:
        attack = int(match.group(1))
        health = int(match.group(2)) if match.group(2) else 0
        if "Health" in match.group(0) and not match.group(2):
            attack, health = 0, attack
        spec["on_kill_buff"] = {"attack": attack, "health": health}

    match = RE_HERO_WHEN_SPACE.search(text)
    if match:
        spec["when_space"] = {"copy": match.group(1).lower()}
        unlock = RE_HERO_UNLOCK.search(text)
        if unlock:
            spec["when_space"]["unlock_turn"] = int(unlock.group(1))
    return spec


def _is_battlegrounds(card: dict[str, Any]) -> bool:
    card_id = card.get("id", "")
    if card.get("techLevel"):
        return True
    if card.get("isBattlegroundsPoolMinion") or card.get("battlegroundsPremiumDbfId"):
        return True
    return card_id.startswith(("BG", "TB_Bacon", "BGS_", "TB_BaconUps"))


# --------------------------------------------------------------------------
# text -> effect spec
# --------------------------------------------------------------------------

COUNT = r"(a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+)"

# Clause markers used to slice a card's text into the part belonging to each
# trigger, so "Avenge (2) ... Deathrattle: ..." does not bleed together. Bare
# keyword lines ("Taunt.", "Reborn.") are deliberately *not* markers: they
# always precede the triggers in the printed text, and cutting on them would
# truncate payloads that end in a keyword ("Give a friendly Undead Reborn.").
CLAUSE_MARKERS = ("Deathrattle:", "Avenge (", "Start of Combat:", "Battlecry:",
                  "Frenzy:", "Rally:")

RE_CLEAVE = re.compile(r"damages the minions next to wh", re.I)
RE_EXCESS = re.compile(r"deal excess damage to (?:an adjacent|both adjacent)", re.I)

RE_SUMMON_STATS = re.compile(
    rf"Summon {COUNT}\s+(?:random\s+)?(?:Golden\s+)?(\d+)/(\d+)\s*([A-Za-z'\- ]*)", re.I)
RE_SUMMON_NAMED = re.compile(
    rf"Summon (?:and get )?{COUNT}\s+(?:random\s+)?(Golden\s+)?"
    r"([A-Za-z][A-Za-z'\- ]*?)(?=\s*(?:\.|,|;|$|\swith\s|\sthat\s|\sand\s|\sfrom\s|\sequal\s))",
    re.I)
RE_SOURCE_STATS = re.compile(r"with (?:this minion's|their|its) maximum stats", re.I)
RE_GIVE_THEM_TAUNT = re.compile(r"[Gg]ive (?:them|it) Taunt", re.I)

# "Summon a random Beast", "Summon 2 random Deathrattle minions"
RE_SUMMON_POOL = re.compile(
    rf"Summon (?:and get )?{COUNT} random ([A-Za-z' ]+?)"
    rf"(?=\s*(?:\.|,|;|$|\sfrom\s|\swith\s|\sand\s))", re.I)
RE_SUMMON_TIERS = re.compile(r"from Tiers? ([\d, and]+)", re.I)
RE_SET_STATS = re.compile(r"Set (?:its|their) stats to (\d+)/(\d+)", re.I)
# "Summon a number of 1/1 Rats equal to this minion's Attack"
RE_SUMMON_PER_ATTACK = re.compile(
    r"Summon a number of (\d+)/(\d+) ([A-Za-z'\- ]+?)s? equal to this minion's Attack", re.I)
# "Summon plain copies of your first 2 Mechs that died this combat"
RE_SUMMON_GRAVEYARD = re.compile(
    rf"Summon plain copies of your first {COUNT} ([A-Za-z]+?)s? that died this combat", re.I)
RE_SUMMON_SELF_COPY = re.compile(r"Summon a copy of this minion", re.I)
# "highest-\nAttack" survives whitespace folding as "highest- Attack"; the
# golden versions summon "the 2 highest-Attack minions" / "the two ... Murlocs".
RE_SUMMON_FROM_HAND = re.compile(
    rf"summon the (?:{COUNT} )?highest-\s*(Attack|Health)\s+([A-Za-z]+?)s?\s+from your hand",
    re.I)
# "Give a random minion in your hand +7/+7 and summon it for this combat only."
RE_SUMMON_RANDOM_FROM_HAND = re.compile(
    rf"[Gg]ive {COUNT} random minion in your hand \+(\d+)/\+(\d+) and summon it", re.I)
RE_IN_HAND = re.compile(r"[Ii]f this minion is in your hand", re.I)
RE_COPY_OF_IT = re.compile(r"summon a copy of it", re.I)
RE_DOUBLE_STATS_COPY = re.compile(r"with double stats", re.I)
RE_HAND_STATS = re.compile(r"[Gg]ain the stats of all the minions in your hand", re.I)

# "Give your Dragons +4/+4", "Give your minions +2/+2", "Give another friendly Beast +6/+6"
MODIFIERS = (r"(?:your|all|another|an|a|other|friendly|random|adjacent|different|"
             r"left-?\s?most|right-?\s?most|odd-Tier|even-Tier|two|three|four|five|six|\d+)")
RE_BUFF_GIVE = re.compile(
    rf"[Gg]ive ((?:{MODIFIERS}\s+)*)([A-Za-z]+?)s?(?: of each type)?\s*\+(\d+)/\+(\d+)", re.I)
RE_BUFF_GIVE_ATK = re.compile(
    rf"[Gg]ive ((?:{MODIFIERS}\s+)*)([A-Za-z]+?)s?\s*\+(\d+) Attack", re.I)
RE_BUFF_GIVE_HP = re.compile(
    rf"[Gg]ive ((?:{MODIFIERS}\s+)*)([A-Za-z]+?)s?\s*\+(\d+) Health", re.I)
# "Your Beasts have +8/+8", "your odd-Tier minions have +3/+3"
RE_BUFF_HAVE = re.compile(
    rf"[Yy]our ((?:{MODIFIERS}\s+)*)([A-Za-z]+?)s? have \+(\d+)/\+(\d+)", re.I)
RE_BUFF_HAVE_ATK = re.compile(
    rf"[Yy]our ((?:{MODIFIERS}\s+)*)([A-Za-z]+?)s? have \+(\d+) Attack", re.I)

RE_GIVE_IT = re.compile(r"give it \+(\d+)/\+(\d+)", re.I)
RE_GIVE_IT_ATK = re.compile(r"give it \+(\d+) Attack", re.I)
RE_GAIN_BOTH = re.compile(r"(?:gains?|[Ii]mprove this by) \+(\d+)/\+(\d+)", re.I)
RE_GAIN_ATK = re.compile(r"gains? \+(\d+) Attack", re.I)
RE_GAIN_HP = re.compile(r"gains? \+(\d+) Health", re.I)
RE_HAVE_ATK = re.compile(r"minions have \+(\d+) Attack for the rest", re.I)

KEYWORDS = ("Divine Shield", "Venomous", "Poisonous", "Reborn", "Windfury", "Taunt", "Stealth")
_KEYWORD_ALT = "|".join(KEYWORDS)
# "Give a random friendly minion Divine Shield", "Give 3 friendly Dragons Divine Shield"
RE_GRANT = re.compile(
    rf"[Gg]ive ((?:{MODIFIERS}\s+)*)([A-Za-z]+?)s? ((?:{_KEYWORD_ALT})"
    rf"(?:(?:,| and|,and)?\s*(?:{_KEYWORD_ALT}))*)", re.I)

# "Give this minion's Attack to a random friendly minion"
RE_TRANSFER = re.compile(
    rf"[Gg]ive this minion's (maximum stats|maximum Health|Attack|Health) to "
    rf"((?:{MODIFIERS}\s+)*)([A-Za-z]+?)s?\b", re.I)
# "Give 2 different friendly minions this minion's Attack"
RE_TRANSFER_REVERSED = re.compile(
    rf"[Gg]ive ((?:{MODIFIERS}\s+)*)([A-Za-z]+?)s? this minion's "
    r"(maximum stats|maximum Health|Attack|Health)", re.I)
# "Give another friendly Pirate Health equal to this minion's Attack"
RE_TRANSFER_EQUAL = re.compile(
    rf"[Gg]ive ((?:{MODIFIERS}\s+)*)([A-Za-z]+?)s? (Attack|Health) equal to "
    r"(?:this minion's|its) (Attack|Health)", re.I)

RE_DESTROY_KILLER = re.compile(r"Destroy the minion that killed this", re.I)
RE_BUFF_KILLER = re.compile(r"[Gg]ive the minion that killed this \+(\d+)/\+(\d+)", re.I)

RE_DMG_RANDOM = re.compile(
    rf"[Dd]eal (\d+) damage to (?:a random enemy minion|{COUNT} random enemy minions)", re.I)
RE_DMG_ALL = re.compile(
    r"[Dd]eal (\d+) damage to all minions(?:\s*\(?except friendly ([A-Za-z]+?)s?\)?)?", re.I)
RE_DMG_ALL_ENEMY = re.compile(r"[Dd]eal (\d+) damage to all enemy minions", re.I)
RE_DMG_FRIENDLY = re.compile(
    r"[Dd]eal (\d+) damage to (?:your (other )?minions|all (other )?friendly minions|them)",
    re.I)
RE_DMG_EXTREME = re.compile(
    r"[Dd]eal (\d+) damage to the (lowest|highest)-Health enemy minion", re.I)
RE_DMG_NEAREST = re.compile(
    rf"[Dd]eal (\d+) damage to the {COUNT} nearest enemy minions?", re.I)
RE_DMG_ADJACENT = re.compile(r"[Dd]eal (\d+) damage to (?:the )?adjacent minions", re.I)
RE_DMG_BY_ATTACK = re.compile(
    r"[Dd]eal damage equal to this minion's Attack to a random enemy minion", re.I)

RACE_WORDS = {
    "beast": "BEAST", "beasts": "BEAST", "murloc": "MURLOC", "demon": "DEMON",
    "dragon": "DRAGON", "mech": "MECHANICAL", "mechs": "MECHANICAL",
    "elemental": "ELEMENTAL", "pirate": "PIRATE", "quilboar": "QUILBOAR",
    "naga": "NAGA", "undead": "UNDEAD",
}

# What a trigger can watch: any minion, or one tribe of them. Cards word the
# tribal variant exactly like the general one ("whenever a friendly *Beast*
# dies"), so the patterns below take either and keep the tribe as a filter.
SUBJECT = "(?:minion|" + "|".join(sorted(RACE_WORDS, key=len, reverse=True)) + ")s?"

RE_BLOOD_GEMS = re.compile(
    rf"plays {COUNT} (?:permanent )?Blood Gems? on (?:all )?your ((?:other )?[A-Za-z]+)", re.I)
# "Whenever another friendly minion attacks, this plays a Blood Gem on it."
# "Whenever a friendly Beast attacks, give your Beasts +4/+2." — same trigger,
# a tribe filter, and a payload aimed at the board rather than at the attacker.
RE_ALLY_ATTACK = re.compile(
    rf"(?:Whenever|After) (another |a different |a |an |your )?friendly "
    rf"({SUBJECT}) attacks,(.*?)(?:\.|$)", re.I)
RE_PLAYS_GEM_ON_IT = re.compile(r"plays (a|\d+) Blood Gems? on it", re.I)
# "give it +3 Attack" — the minion that just swung, not the whole warband. The
# distinction matters: read as a board buff, Ripsnarl Captain would hand +3 to
# six minions per attack instead of one. (RE_GIVE_IT above pulls the numbers out
# of the same wording for summon watchers; this one only has to spot it.)
RE_GIVE_TO_IT = re.compile(r"\bgive it\b", re.I)
# "Rally: This plays a Blood Gem on itself." — the Quilboar wording for a plain
# self buff, and worth its own pattern: Tusked Camper is a tier-1 minion, so an
# unparsed one drags the confidence of nearly every early fight down.
RE_PLAYS_GEM_SELF = re.compile(
    r"plays (a|an|\d+) (?:permanent )?Blood Gems? on (?:itself|this minion|this)\b", re.I)
RE_BLOOD_GEMS_ADJ = re.compile(
    r"plays a (?:permanent )?Blood Gem on adjacent minions", re.I)
RE_DEATH_TRIBE = re.compile(
    rf"(?:Whenever|After) (?:a |an |another |a different )?friendly ({SUBJECT}) dies", re.I)
# Greybough's "Sprout It Out!" and anything else worded like it.
RE_SUMMON_BUFF = re.compile(
    r"Give \+(\d+)/\+(\d+)(?: and (Taunt|Divine Shield|Windfury))? "
    r"to minions you summon during combat", re.I)
# Rokara: "After a friendly minion kills an enemy, give it +1 Attack permanently."
RE_HERO_KILL_BUFF = re.compile(
    r"After a friendly minion kills an enemy, give it \+(\d+)"
    r"(?:/\+(\d+))?(?: Attack| Health)?", re.I)
# Drek'Thar and Vanndar Stormpike: a body that walks in the moment a slot opens.
RE_HERO_WHEN_SPACE = re.compile(
    r"When you have space in combat, summon a copy of your highest-(Attack|Health) minion",
    re.I)
RE_HERO_UNLOCK = re.compile(r"Unlocks on Turn (\d+)", re.I)
# "When you have space, summon ..." — a body that walks in the instant a slot
# frees up, so it can only be modelled mid-fight. Trinkets are the main source,
# and an unmodelled one shifts every slot index after it: the replay in
# tools/divergence.py named Automaton Portrait's 3/4 in a third of all the
# attack-order disagreements it found.
RE_WHEN_SPACE = re.compile(r"[Ww]hen you have space,?\s*(summon[^.]*)", re.I)
RE_EXACT_COPY_DEAD = re.compile(
    r"summon an exact copy of your first ([A-Za-z]+?) that died", re.I)
# --- Rally payloads that act on the minion this one just attacked ----------
# The engine knows the attack target, so "the target" is a real referent rather
# than a guess. Whole archetypes hang off this wording — Sin'dorei Straight Shot
# stripping Taunt, Transmuted Bramblewitch shrinking a 90/90 to 3/3 — and read
# as unmodelled they turned decided fights into coin flips.
RE_TARGET_DMG = re.compile(r"[Dd]eal (\d+) damage to the target\b(?!'s)", re.I)
RE_TARGET_DMG_NEIGHBOURS = re.compile(
    r"[Dd]eal (\d+) damage to the target's neighbou?rs", re.I)
RE_TARGET_DMG_BY_ATTACK = re.compile(
    r"[Dd]eal damage equal to this minion's Attack to the target"
    r"( and an adjacent minion)?", re.I)
RE_TARGET_SET_STATS = re.compile(r"Set the target's stats to (\d+)/(\d+)", re.I)
RE_TARGET_REMOVE = re.compile(
    rf"Remove ((?:{_KEYWORD_ALT})(?:(?:,| and|,and)?\s*(?:{_KEYWORD_ALT}))*) "
    rf"from the target", re.I)
RE_TARGET_GAIN_ATTACK = re.compile(r"[Gg]ain the target's Attack", re.I)
RE_DESTROY_SELF = re.compile(r"attacks,? destroy it", re.I)
# "Gain Attack equal to your Tier permanently" (Hydralisk).
RE_GAIN_TIER_ATTACK = re.compile(r"[Gg]ain Attack equal to your Tier", re.I)

# --- positional payloads ---------------------------------------------------
# "the minion to the right of this", "your left-most minion", "an adjacent
# minion". Position matters more than it looks: Monstrous Macaw re-firing the
# left-most Deathrattle is one of the strongest boards in the game.
RE_POSITION = re.compile(
    r"(?:the minion to the (right|left)(?: of this)?"
    r"|your (left|right)-?\s?most minion"
    r"|(?:an |the )?adjacent minion)", re.I)
RE_TRIGGER_DEATHRATTLE = re.compile(
    r"[Tt]rigger (?:your |an? |the )?(left-?\s?most|right-?\s?most|adjacent)"
    r"[^.]*?Deathrattle", re.I)
RE_MAKE_GOLDEN = re.compile(
    r"[Mm]ake your (left|right)-?\s?most minion Golden", re.I)
# "Your Deathrattles trigger an extra time." — Titus Rivendare's aura, which
# multiplies every deathrattle on the board rather than carrying one of its own.
RE_DEATHRATTLE_AURA = re.compile(
    r"[Yy]our Deathrattles trigger (?:(\w+) extra times?|an extra time)", re.I)
# "After a friendly Rally minion attacks, trigger your left-most Deathrattle."
# The watcher reacts to somebody else's Rally, so it needs the ally-attack hook
# with a Rally filter rather than its own "after this attacks".
RE_RALLY_ALLY_ATTACK = re.compile(
    r"After a friendly Rally minion attacks,(.*?)(?:\.|$)", re.I)
# "Your Beetles have +5/+5 this game." — not a buff on the bodies standing here
# but on every Beetle summoned from now on, which is the whole engine behind a
# Beetle board. Read as a plain buff it hit the warband; dropped, as it was, the
# board simulated as if its tokens never grew at all.
RE_GAME_BUFF = re.compile(
    r"[Yy]our ([A-Za-z]+?)s? have \+(\d+)/\+(\d+) this game", re.I)

RE_TWICE = re.compile(r",?\s*\btwice\b", re.I)
RE_DOUBLE_STATS = re.compile(r"(Double|Triple) this minion's stats", re.I)
RE_LIMIT = re.compile(rf"\({COUNT} times? per combat", re.I)
RE_ONCE = re.compile(r"\(Once per combat", re.I)

POOL_KINDS = {
    "deathrattle": {"mechanic": "DEATHRATTLE"},
    "legendary": {"legendary": True},
    "divine shield": {"mechanic": "DIVINE_SHIELD"},
    "taunt": {"mechanic": "TAUNT"},
    "battlecry": {"mechanic": "BATTLECRY"},
    "rally": {"mechanic": "BACON_RALLY"},
    "reborn": {"mechanic": "REBORN"},
}

# --------------------------------------------------------------------------
# text normalisation
# --------------------------------------------------------------------------

# "Battlecry, Deathrattle, and Rally: Give your other minions +2/+2." — one
# payload wired to several triggers. Left as printed, the clause slicer found no
# "Deathrattle:" marker and the card came out as a plain vanilla body.
TRIGGER_NAMES = r"Battlecry|Deathrattle|Rally|Frenzy|Start of Combat|Avenge \(\d+\)"
RE_SHARED_TRIGGER = re.compile(
    rf"\b((?:{TRIGGER_NAMES})(?:\s*,\s*(?:and\s+)?|\s+and\s+)"
    rf"(?:{TRIGGER_NAMES})(?:\s*,\s*(?:and\s+)?|\s+and\s+)?)*"
    rf"((?:{TRIGGER_NAMES}))\s*:\s*", re.I)
RE_TRIGGER_NAME = re.compile(rf"(?:{TRIGGER_NAMES})", re.I)
# "Rally: Cast Queen's Command." — the spell it names is usually a plain buff.
RE_CAST_SPELL = re.compile(r"[Cc]asts? ([A-Z][A-Za-z'’\- ]+?)(?=\s*(?:\.|,|;|$|\son\b))")
# Wording that describes minions arriving later in the game rather than the
# bodies on the board now ("Your Beetles have +2/+2 this game").
RE_THIS_GAME = re.compile(r"[^.]*\bthis game\b[^.]*\.?", re.I)


def _inline_spells(text: str, spells: dict[str, str]) -> str:
    """Replace "Cast <Spell>" with what the spell actually does."""
    if not spells or "ast " not in text:
        return text

    def swap(match: "re.Match[str]") -> str:
        body = spells.get(match.group(1).strip().lower())
        return body if body else match.group(0)

    return RE_CAST_SPELL.sub(swap, text)


def _expand_shared_triggers(text: str) -> str:
    """Rewrite one payload shared by several triggers into a sentence each."""
    match = RE_SHARED_TRIGGER.search(text)
    if not match:
        return text
    names = RE_TRIGGER_NAME.findall(match.group(0))
    if len(names) < 2:
        return text
    rest = text[match.end():]
    cut = len(rest)
    for marker in CLAUSE_MARKERS:
        pos = rest.find(marker)
        if 0 <= pos < cut:
            cut = pos
    payload, tail = rest[:cut].strip(), rest[cut:]
    if not payload:
        return text
    if not payload.endswith("."):
        payload += "."
    expanded = " ".join(f"{name}: {payload}" for name in names)
    return f"{text[:match.start()]}{expanded} {tail}".strip()


def _normalise(text: str, spells: Optional[dict[str, str]] = None) -> str:
    return _expand_shared_triggers(_inline_spells(text, spells or {}))


def _position_of(clause: str) -> Optional[str]:
    """Which slot a payload names: "right", "left", "leftmost", "rightmost",
    "adjacent" — or ``None`` when it names no slot at all."""
    match = RE_POSITION.search(clause)
    if not match:
        return None
    if match.group(1):
        return match.group(1).lower()
    if match.group(2):
        return match.group(2).lower() + "most"
    return "adjacent"


def _parse_target(clause: str) -> dict[str, Any]:
    """Rally payloads aimed at the minion this one just attacked."""
    out: dict[str, Any] = {}
    damage: list[dict[str, Any]] = []

    match = RE_TARGET_DMG.search(clause)
    if match:
        damage.append({"amount": int(match.group(1)), "mode": "target"})
    match = RE_TARGET_DMG_NEIGHBOURS.search(clause)
    if match:
        damage.append({"amount": int(match.group(1)), "mode": "target_neighbours"})
    match = RE_TARGET_DMG_BY_ATTACK.search(clause)
    if match:
        damage.append({"source_attack": True,
                       "mode": "target_and_neighbour" if match.group(1) else "target"})
    if damage:
        out["target_damage"] = damage

    match = RE_TARGET_SET_STATS.search(clause)
    if match:
        out["set_target_stats"] = [int(match.group(1)), int(match.group(2))]

    match = RE_TARGET_REMOVE.search(clause)
    if match:
        out["remove_target_keywords"] = [k.lower().replace(" ", "_") for k in KEYWORDS
                                         if re.search(rf"\b{k}\b", match.group(1), re.I)]

    if RE_TARGET_GAIN_ATTACK.search(clause):
        out["gain_target_attack"] = True
    return out


def _race_of(word: Optional[str]) -> Optional[str]:
    if not word:
        return None
    word = word.strip().lower()
    # The table lists the singular; card text uses whichever reads better, and
    # "4 random Pirates" resolving to no tribe silently summoned nothing.
    return RACE_WORDS.get(word) or (RACE_WORDS.get(word[:-1]) if word.endswith("s") else None)


def _clause(text: str, marker: str) -> str:
    """The part of the text belonging to one trigger."""
    idx = text.find(marker)
    if idx < 0:
        return ""
    start = idx + len(marker)
    if marker == "Avenge (":  # skip "N):"
        close = text.find(":", start)
        if close < 0:
            return ""
        start = close + 1
    rest = text[start:]
    cut = len(rest)
    for other in CLAUSE_MARKERS:
        if other == marker:
            continue
        pos = rest.find(other)
        if 0 <= pos < cut:
            cut = pos
    return rest[:cut].strip()


def _trigger_clause(text: str, pattern: str) -> Optional[str]:
    """Everything after a "Whenever ..., " style trigger, up to the sentence end."""
    match = re.search(pattern, text, re.I)
    if not match:
        return None
    rest = text[match.end():].lstrip(" ,")
    cut = len(rest)
    for marker in CLAUSE_MARKERS:
        pos = rest.find(marker)
        if 0 <= pos < cut:
            cut = pos
    return rest[:cut].strip()


def _limit_of(clause: str) -> int:
    if RE_ONCE.search(clause):
        return 1
    match = RE_LIMIT.search(clause)
    return _num(match.group(1)) if match else 0


def _target_group(modifiers: str, noun: str) -> dict[str, Any]:
    """Read "2 random friendly Dragons" into race / count / exclude_self."""
    modifiers = (modifiers or "").lower()
    noun = (noun or "").strip().lower()
    race = _race_of(noun)
    group: dict[str, Any] = {"race": race}
    group["exclude_self"] = any(w in modifiers for w in ("other", "another", "different"))
    if "adjacent" in modifiers:
        group["adjacent"] = True
    if "odd-tier" in modifiers:
        group["tier_parity"] = "odd"
    elif "even-tier" in modifiers:
        group["tier_parity"] = "even"
    count = 0
    if "your" not in modifiers and "all" not in modifiers:
        for word in modifiers.split():
            value = NUMBER_WORDS.get(word, int(word) if word.isdigit() else 0)
            if value:
                count = value
                break
    group["count"] = count
    return group


def _token_from_name(name: str, index: dict[str, dict[str, Any]],
                     golden: bool) -> Optional[dict[str, Any]]:
    """Resolve 'Scallywags' -> the Scallywag card, and read its real stats."""
    key = name.strip().lower()
    card = index.get(key)
    if card is None and key.endswith("es"):
        card = index.get(key[:-2])
    if card is None and key.endswith("s"):
        card = index.get(key[:-1])
    if card is None:
        return None
    mechanics = set(card.get("mechanics") or [])
    attack = int(card.get("attack") or 1)
    health = int(card.get("health") or 1)
    if golden:
        attack *= 2
        health *= 2
    races = card.get("races") or ([card["race"]] if card.get("race") else [])
    return {
        "card_id": card.get("id", ""),
        "name": card.get("name", name),
        "attack": attack,
        "health": health,
        "tier": int(card.get("techLevel") or 1),
        # A summoned Beetle is a Beast, and every tribal buff on the board cares.
        # Tokens used to arrive with no tribe at all, so "give your Beasts +4/+4"
        # skipped the very bodies the card had just made.
        "races": tuple(races),
        "taunt": "TAUNT" in mechanics,
        "divine_shield": "DIVINE_SHIELD" in mechanics,
        "poisonous": "POISONOUS" in mechanics,
        "venomous": "VENOMOUS" in mechanics,
        "stealth": "STEALTH" in mechanics,
        "windfury": "WINDFURY" in mechanics,
        "reborn": "REBORN" in mechanics,
    }


def _parse_pool_summon(clause: str) -> Optional[dict[str, Any]]:
    """"Summon 2 random Deathrattle minions", "Summon a random Beast from Tiers 2, 3, and 4"."""
    match = RE_SUMMON_POOL.search(clause)
    if not match:
        return None
    what = match.group(2).strip().lower()
    filters: dict[str, Any] = {"max_tier": 6}
    for kind, spec in POOL_KINDS.items():
        if what.startswith(kind):
            filters.update(spec)
            break
    else:
        race = _race_of(what.split()[0]) if what.split() else None
        if race is None:
            if not what.startswith("minion"):
                return None
        else:
            filters["race"] = race
    tiers = RE_SUMMON_TIERS.search(clause)
    if tiers:
        found = [int(n) for n in re.findall(r"\d+", tiers.group(1))]
        if found:
            filters["tiers"] = found
            filters.pop("max_tier", None)
    summon: dict[str, Any] = {"count": _num(match.group(1)), "pool": filters}
    stats = RE_SET_STATS.search(clause)
    if stats:
        summon["set_stats"] = [int(stats.group(1)), int(stats.group(2))]
    return summon


def _parse_summon(clause: str, index: dict[str, dict[str, Any]]) -> Optional[dict[str, Any]]:
    if "summon" not in clause.lower():
        return None

    match = RE_SUMMON_PER_ATTACK.search(clause)
    if match:
        return {"count_from_attack": True, "attack": int(match.group(1)),
                "health": int(match.group(2)), "name": match.group(3).strip().title()}

    match = RE_SUMMON_GRAVEYARD.search(clause)
    if match:
        return {"count": _num(match.group(1)),
                "from_graveyard": {"race": _race_of(match.group(2))}}

    if RE_SUMMON_SELF_COPY.search(clause):
        return {"count": 1, "copy_self": True}

    pool = _parse_pool_summon(clause)
    if pool:
        return pool

    # Explicit stat line wins: "Summon two 1/1 Beasts."
    match = RE_SUMMON_STATS.search(clause)
    if match:
        name = (match.group(4) or "Token").strip().title() or "Token"
        summon = {
            "count": _num(match.group(1)),
            "attack": int(match.group(2)),
            "health": int(match.group(3)),
            "name": name,
            "taunt": bool(RE_GIVE_THEM_TAUNT.search(clause)) or "with Taunt" in clause,
        }
        # The printed stats are authoritative, but the *card* behind the name
        # carries the tribe, the tier and the keywords — all of which decide
        # whether tribal buffs and auras see this body at all.
        token = _token_from_name(name, index, golden=False)
        if token is not None:
            summon["card_id"] = token["card_id"]
            summon["races"] = token["races"]
            summon["tier"] = token["tier"]
            for flag in ("divine_shield", "poisonous", "venomous", "reborn",
                         "windfury", "stealth"):
                if token.get(flag):
                    summon[flag] = True
            summon["taunt"] = summon["taunt"] or bool(token.get("taunt"))
        return summon
    match = RE_SUMMON_NAMED.search(clause)
    if match:
        token = _token_from_name(match.group(3), index, golden=bool(match.group(2)))
        if token is not None:
            token["count"] = _num(match.group(1))
            if RE_GIVE_THEM_TAUNT.search(clause):
                token["taunt"] = True
            return token
    if RE_SOURCE_STATS.search(clause):
        match = re.search(rf"Summon {COUNT}", clause, re.I)
        return {"count": _num(match.group(1)) if match else 1, "use_source_stats": True,
                "attack": 1, "health": 1, "name": "Copy"}
    return None


def _parse_buff(clause: str) -> Optional[dict[str, Any]]:
    """Stat buffs handed to friendly minions, in any of the printed wordings."""
    # "Your Beetles have +2/+2 this game" is about the Beetles you summon from
    # now on, not the bodies standing here — read as a payload it handed the
    # whole warband +2/+2 every time the card died.
    clause = RE_THIS_GAME.sub(" ", clause)
    attack = health = 0
    group: Optional[dict[str, Any]] = None
    for regex, kind in ((RE_BUFF_GIVE, "both"), (RE_BUFF_HAVE, "both"),
                        (RE_BUFF_GIVE_ATK, "attack"), (RE_BUFF_HAVE_ATK, "attack"),
                        (RE_BUFF_GIVE_HP, "health")):
        match = regex.search(clause)
        if not match:
            continue
        group = _target_group(match.group(1), match.group(2))
        if kind == "both":
            attack, health = int(match.group(3)), int(match.group(4))
        elif kind == "attack":
            attack = int(match.group(3))
        else:
            health = int(match.group(3))
        break

    gems = RE_BLOOD_GEMS.search(clause)
    if gems and group is None:
        # A Blood Gem is +1/+1, so N gems on a tribe is a flat tribal buff.
        # "on all your other minions" names no tribe and skips the caster.
        noun = gems.group(2).strip().lower()
        group = {"race": _race_of(noun.replace("other ", "")), "count": 0,
                 "exclude_self": noun.startswith("other")}
        attack = health = _num(gems.group(1))
    if RE_BLOOD_GEMS_ADJ.search(clause) and group is None:
        group = {"race": None, "count": 0, "adjacent": True, "exclude_self": True}
        attack = health = 1

    if group is None or (attack == 0 and health == 0):
        return _parse_position_buff(clause)
    buff = dict(group)
    buff["attack"] = attack
    buff["health"] = health
    if re.search(r"of each type", clause, re.I):
        # "a friendly minion of each type": one target per tribe on the board.
        buff["per_type"] = True
        buff["count"] = 0
    # "+2/+2 and Divine Shield" rides along with the stats.
    for keyword in KEYWORDS:
        if re.search(rf"\+\d+(?:/\+\d+)?[^.]*\b{keyword}\b", clause, re.I):
            buff[keyword.lower().replace(" ", "_")] = True
    return buff


# "Give the minion to the right of this +2/+2" — a slot, not a tribe, so the
# general "give <group> +N/+N" pattern cannot see it at all.
RE_BUFF_POSITION = re.compile(
    r"[Gg]ive (?:the minion to the (right|left)(?: of this)?"
    r"|your (left|right)-?\s?most minion|(?:an |the )?adjacent minions?) "
    r"\+(\d+)/\+(\d+)", re.I)
RE_GRANT_POSITION = re.compile(
    rf"[Gg]ive (?:the minion to the (right|left)(?: of this)?"
    rf"|your (left|right)-?\s?most minion|(?:an |the )?adjacent minions?) "
    rf"((?:{_KEYWORD_ALT})(?:(?:,| and|,and)?\s*(?:{_KEYWORD_ALT}))*)", re.I)


def _slot_of(match: "re.Match[str]") -> str:
    if match.group(1):
        return match.group(1).lower()
    if match.group(2):
        return match.group(2).lower() + "most"
    return "adjacent"


def _parse_position_buff(clause: str) -> Optional[dict[str, Any]]:
    match = RE_BUFF_POSITION.search(clause)
    if not match:
        return None
    return {"race": None, "count": 1, "exclude_self": True,
            "position": _slot_of(match), "attack": int(match.group(3)),
            "health": int(match.group(4))}


def _parse_grants(clause: str) -> list[dict[str, Any]]:
    """"Give a random friendly minion Divine Shield" and friends."""
    out: list[dict[str, Any]] = []
    slot = RE_GRANT_POSITION.search(clause)
    if slot:
        keywords = [k.lower().replace(" ", "_") for k in KEYWORDS
                    if re.search(rf"\b{k}\b", slot.group(3), re.I)]
        if keywords:
            return [{"keywords": keywords, "count": 1, "race": None,
                     "exclude_self": True, "side": "friendly",
                     "position": _slot_of(slot)}]
    for match in RE_GRANT.finditer(clause):
        words = match.group(3)
        keywords = [k.lower().replace(" ", "_") for k in KEYWORDS
                    if re.search(rf"\b{k}\b", words, re.I)]
        if not keywords:
            continue
        group = _target_group(match.group(1), match.group(2))
        out.append({"keywords": keywords, "count": max(1, int(group.get("count", 1))),
                    "race": group.get("race"), "exclude_self": group.get("exclude_self", True),
                    "side": "friendly"})
    return out


def _parse_transfer(clause: str) -> Optional[dict[str, Any]]:
    stat_map = {"attack": "attack", "health": "health",
                "maximum health": "health", "maximum stats": "both"}
    match = RE_TRANSFER.search(clause)
    if match:
        group = _target_group(match.group(2), match.group(3))
        stat = stat_map.get(match.group(1).lower(), "attack")
    else:
        match = RE_TRANSFER_REVERSED.search(clause)
        if match:
            group = _target_group(match.group(1), match.group(2))
            stat = stat_map.get(match.group(3).lower(), "attack")
        else:
            match = RE_TRANSFER_EQUAL.search(clause)
            if not match:
                return None
            group = _target_group(match.group(1), match.group(2))
            given, source_stat = match.group(3).lower(), match.group(4).lower()
            transfer = {"stat": source_stat, "count": max(1, int(group.get("count", 1))),
                        "race": group.get("race")}
            if given != source_stat:
                transfer["attack_as_health"] = given == "health"
            return transfer
    count = max(1, int(group.get("count", 1)))
    if RE_TWICE.search(clause):
        count *= 2
    return {"stat": stat, "count": count, "race": group.get("race")}


def _parse_damage(clause: str) -> list[dict[str, Any]]:
    """Every "deal N damage to ..." wording the cards actually use."""
    actions: list[dict[str, Any]] = []
    twice = 2 if RE_TWICE.search(clause) else 1

    match = RE_DMG_RANDOM.search(clause)
    if match:
        count = _num(match.group(2)) if match.group(2) else 1
        actions.append({"amount": int(match.group(1)), "count": count * twice,
                        "mode": "random", "side": "enemy"})

    match = RE_DMG_ALL_ENEMY.search(clause)
    if match:
        actions.append({"amount": int(match.group(1)), "mode": "all", "side": "enemy"})

    match = RE_DMG_ALL.search(clause)
    if match:
        action = {"amount": int(match.group(1)), "mode": "all", "side": "all"}
        if match.group(2):
            action["except_race"] = _race_of(match.group(2))
        actions.append(action)
    elif RE_DMG_FRIENDLY.search(clause):
        match = RE_DMG_FRIENDLY.search(clause)
        actions.append({"amount": int(match.group(1)), "mode": "all", "side": "friendly"})

    match = RE_DMG_EXTREME.search(clause)
    if match:
        actions.append({"amount": int(match.group(1)), "count": twice,
                        "mode": f"{match.group(2).lower()}_health", "side": "enemy"})

    match = RE_DMG_NEAREST.search(clause)
    if match:
        actions.append({"amount": int(match.group(1)), "count": _num(match.group(2)),
                        "mode": "nearest", "side": "enemy"})

    match = RE_DMG_ADJACENT.search(clause)
    if match:
        actions.append({"amount": int(match.group(1)), "mode": "adjacent", "side": "friendly"})

    if RE_DMG_BY_ATTACK.search(clause):
        actions.append({"source_attack": True, "count": twice,
                        "mode": "random", "side": "enemy"})

    return actions


def _parse_gain(clause: str) -> Optional[dict[str, Any]]:
    gain: dict[str, Any] = {}
    match = RE_GAIN_BOTH.search(clause)
    if match:
        gain = {"attack": int(match.group(1)), "health": int(match.group(2))}
    else:
        match = RE_GAIN_ATK.search(clause)
        if match:
            gain = {"attack": int(match.group(1)), "health": 0}
        else:
            match = RE_GAIN_HP.search(clause)
            if match:
                gain = {"attack": 0, "health": int(match.group(1))}
    # "Gain Taunt and Reborn", "Gain Divine Shield"
    keyword_clause = re.search(r"[Gg]ains? ([A-Za-z, ]*?(?:Taunt|Reborn|Divine Shield|Windfury)"
                               r"[A-Za-z, ]*)", clause)
    if keyword_clause:
        words = keyword_clause.group(1)
        for key, flag in (("Taunt", "taunt"), ("Reborn", "reborn"),
                          ("Divine Shield", "divine_shield"), ("Windfury", "windfury")):
            if key in words:
                gain[flag] = True
    return gain or None


def _parse_payload(clause: str, index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The shared summon / buff / grant / damage vocabulary of every trigger."""
    payload: dict[str, Any] = {}
    hand_summon = RE_SUMMON_FROM_HAND.search(clause)
    if hand_summon:
        # "the two highest-Attack Murlocs from your hand" — the noun is the
        # filter, and a plain "minion" means no filter at all. Bassgill picks by
        # Health instead, which changes which card leaves the hand.
        payload["summon_from_hand"] = {
            "count": _num(hand_summon.group(1)) if hand_summon.group(1) else 1,
            "by": hand_summon.group(2).lower(),
            "race": _race_of(hand_summon.group(3).lower()),
        }
    random_hand = RE_SUMMON_RANDOM_FROM_HAND.search(clause)
    if random_hand and "summon_from_hand" not in payload:
        payload["summon_from_hand"] = {
            "count": _num(random_hand.group(1)), "by": "random", "race": None,
            "buff": {"attack": int(random_hand.group(2)),
                     "health": int(random_hand.group(3))},
        }
    summon = _parse_summon(clause, index)
    if summon and "summon_from_hand" not in payload:
        payload["summon"] = summon
    buff = _parse_buff(clause)
    if buff:
        payload["buff"] = buff
    grants = _parse_grants(clause)
    if grants:
        payload["grant"] = grants
    damage = _parse_damage(clause)
    if damage:
        payload["damage"] = damage
    transfer = _parse_transfer(clause)
    if transfer:
        payload["transfer"] = transfer

    # "Trigger your left-most Deathrattle" — Monstrous Macaw and Warghoul fire
    # somebody else's deathrattle without anything dying, which no other payload
    # in this vocabulary can express.
    fire = RE_TRIGGER_DEATHRATTLE.search(clause)
    if fire:
        payload["trigger_deathrattle"] = {
            "position": fire.group(1).lower().replace("-", "").replace(" ", ""),
            "count": 2 if RE_TWICE.search(clause) else 1,
        }
    golden = RE_MAKE_GOLDEN.search(clause)
    if golden:
        # Golden is exactly double stats for a body already on the board.
        payload["make_golden"] = {"position": golden.group(1).lower() + "most"}

    # "Your Beetles have +5/+5 this game" — a standing order on the tokens this
    # board summons later, not a buff on anyone currently standing. _parse_buff
    # above deliberately cuts the sentence out, so it is read separately here.
    game = RE_GAME_BUFF.search(clause)
    if game:
        noun = game.group(1).strip().lower()
        token = _token_from_name(noun, index, golden=False)
        payload["game_buff"] = {
            "token": noun,
            # The token's own card id, so a Beetle is recognised by what it is
            # rather than by how the localised name happens to be spelled.
            "card_id": (token or {}).get("card_id", ""),
            "race": _race_of(noun),
            "attack": int(game.group(2)),
            "health": int(game.group(3)),
        }
    return payload


def parse_card_effects(text: str, mechanics: Iterable[str],
                       index: Optional[dict[str, dict[str, Any]]] = None,
                       spells: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """Extract everything the combat engine can act on from a card's text."""
    index = index or {}
    # "Battlecry, Deathrattle, and Rally: ..." becomes one sentence per trigger,
    # and "Cast Queen's Command" becomes what that spell does — after which the
    # ordinary clause patterns below see wording they already understand.
    text = _normalise(text, spells)
    mechanics = set(mechanics or [])
    spec: dict[str, Any] = {}
    # Each trigger's own words, so the coverage check can tell a deathrattle
    # that reshapes the board from one that only fills your hand.
    clauses: dict[str, str] = {}

    if "DEATHRATTLE" in mechanics:
        spec["has_deathrattle"] = True
    if "BACON_RALLY" in mechanics:
        # Nothing in the log marks Rally, and Deathstrider only reacts to allies
        # that have it — so the keyword has to survive into the spec.
        spec["rally"] = True
    if RE_CLEAVE.search(text) or RE_EXCESS.search(text):
        spec["cleave"] = True

    # --- board-wide auras ------------------------------------------------
    # "Your Deathrattles trigger an extra time." Titus Rivendare owns no
    # payload of his own: he multiplies everybody else's, so the engine reads
    # him off the board at the moment a deathrattle fires.
    aura = RE_DEATHRATTLE_AURA.search(text)
    if aura:
        spec["deathrattle_aura"] = {"extra": _num(aura.group(1) or "an")}

    # --- deathrattle ---------------------------------------------------
    dr_clause = clauses["deathrattle"] = _clause(text, "Deathrattle:")
    if dr_clause:
        deathrattle = _parse_payload(dr_clause, index)
        if RE_DESTROY_KILLER.search(dr_clause):
            deathrattle["destroy_killer"] = True
        killer_buff = RE_BUFF_KILLER.search(dr_clause)
        if killer_buff:
            deathrattle["buff_killer"] = {"attack": int(killer_buff.group(1)),
                                          "health": int(killer_buff.group(2))}
            deathrattle.pop("buff", None)
        if deathrattle:
            spec["deathrattle"] = deathrattle

    # --- start of combat -----------------------------------------------
    soc_clause = _clause(text, "Start of Combat:")
    if soc_clause:
        soc = _parse_payload(soc_clause, index)
        match = RE_DOUBLE_STATS.search(soc_clause)
        if match:
            soc["self_multiplier"] = 2 if match.group(1).lower() == "double" else 3
        if RE_IN_HAND.search(soc_clause):
            # Flighty Scout fires from the hand, not from the board — the
            # engine has to look somewhere else for it entirely.
            soc["in_hand"] = True
            if RE_COPY_OF_IT.search(soc_clause):
                soc["summon"] = {"copy_self": True, "count": 1,
                                 "double_stats": bool(RE_DOUBLE_STATS_COPY.search(soc_clause))}
        if RE_HAND_STATS.search(soc_clause):
            soc["gain_hand_stats"] = True
        if soc:
            spec["start_of_combat"] = soc

    # --- avenge ---------------------------------------------------------
    match = re.search(r"Avenge \((\d+)\)", text)
    if match:
        clause = clauses["avenge"] = _clause(text, "Avenge (")
        avenge: dict[str, Any] = {"threshold": int(match.group(1))}
        avenge.update(_parse_payload(clause, index))
        gain = _parse_gain(clause)
        if gain:
            avenge["gain"] = gain
        if len(avenge) > 1:
            spec["avenge"] = avenge

    # --- frenzy and "whenever this takes damage" -------------------------
    frenzy_clause = _clause(text, "Frenzy:")
    if not frenzy_clause:
        frenzy_clause = _trigger_clause(
            text, r"first time this survives damage(?: each combat)?,?") or ""
    clauses["frenzy"] = frenzy_clause
    if frenzy_clause:
        frenzy = _parse_payload(frenzy_clause, index)
        gain = _parse_gain(frenzy_clause)
        if gain:
            frenzy["gain"] = gain
        if frenzy:
            spec["frenzy"] = frenzy

    damaged_clause = _trigger_clause(text, r"[Ww]henever this (?:minion )?takes damage,")
    clauses["on_damaged"] = damaged_clause or ""
    if damaged_clause:
        damaged = _parse_payload(damaged_clause, index)
        gain = _parse_gain(damaged_clause)
        if gain:
            damaged["gain"] = gain
        if damaged:
            damaged["limit"] = _limit_of(text)
            spec["on_damaged"] = damaged

    # --- kill triggers ---------------------------------------------------
    kill_clause = _trigger_clause(
        text, r"(?:Whenever|After|When) this (?:minion )?(?:attacks and )?kills a minion"
              r"(?: and survives)?,")
    clauses["on_kill"] = kill_clause or ""
    if kill_clause:
        on_kill = _parse_payload(kill_clause, index)
        gain = _parse_gain(kill_clause)
        if gain:
            on_kill["gain"] = gain
        if re.search(r"gain its maximum stats", kill_clause, re.I):
            on_kill["gain_victim_stats"] = True
        if on_kill:
            on_kill["attacking_only"] = "attacks and kills" in text
            on_kill["limit"] = _limit_of(text)
            spec["on_kill"] = on_kill

    # "Whenever you summon a Mech during combat, gain +2 Attack" — only fires on
    # bodies that appear mid-fight (deathrattles, Rally, reborn), which is
    # exactly what the engine's summon hook sees.
    summon_clause = _trigger_clause(
        text, r"[Ww]henever you summon an? ([A-Za-z]+)(?: (?:during|in) combat)?,")
    if summon_clause:
        match = re.search(r"[Ww]henever you summon an? ([A-Za-z]+)", text)
        watcher = _parse_payload(summon_clause, index)
        gain = _parse_gain(summon_clause)
        if gain:
            watcher["gain"] = gain
        give = RE_GIVE_IT.search(summon_clause)
        if give:
            watcher["buff_summoned"] = {"attack": int(give.group(1)),
                                        "health": int(give.group(2))}
        give_atk = RE_GIVE_IT_ATK.search(summon_clause)
        if give_atk:
            watcher["buff_summoned"] = {"attack": int(give_atk.group(1)), "health": 0}
        if "buff_summoned" in watcher:
            # "give *it* ..." is the new body alone; the generic buff parser
            # reads the same words as "buff your minions" and would hit everyone.
            watcher.pop("buff", None)
        grants = _parse_grants(summon_clause)
        if grants:
            watcher["grant"] = grants
        # "gain +2 Attack and Divine Shield" — the keyword lands on the watcher
        # itself, which is not the "give <someone> X" shape _parse_grants reads.
        gained = re.search(r"gain[s]? (?:\+\d+[^.]*?and )?([A-Za-z ]+)", summon_clause, re.I)
        if gained:
            watcher["gain_keywords"] = [k.lower().replace(" ", "_") for k in KEYWORDS
                                        if re.search(rf"\b{k}\b", gained.group(1), re.I)]
        if watcher:
            race = _race_of(match.group(1).lower()) if match else None
            watcher["race"] = race
            spec["on_summon"] = watcher

    friendly_kill = _trigger_clause(text, r"After a friendly minion kills an enemy,")
    if friendly_kill:
        gain = _parse_gain(friendly_kill)
        if gain:
            spec["on_friendly_kill"] = {"gain": gain}

    # --- death / shield triggers ----------------------------------------
    death_clause = _trigger_clause(
        text, rf"(?:Whenever|After) (?:a |an |another |a different )?(?:friendly )?"
              rf"(?:Deathrattle )?{SUBJECT} dies(?: in combat)?,")
    clauses["on_friendly_death"] = death_clause or ""
    if death_clause:
        trigger = _parse_payload(death_clause, index)
        gain = _parse_gain(death_clause)
        if gain:
            trigger["gain"] = gain
        tribe = RE_DEATH_TRIBE.search(text)
        if tribe:
            # "whenever a friendly Beast dies" only counts Beasts.
            trigger["race"] = _race_of(tribe.group(1))
        if re.search(r"gain its maximum stats", death_clause, re.I):
            trigger["gain_dead_stats"] = True
        elif re.search(r"gain its Attack", death_clause, re.I):
            trigger["gain_dead_attack"] = True
        if re.search(r"gain its Deathrattle", death_clause, re.I):
            # Fish of N'Zoth stacks every deathrattle that dies beside it, and
            # a board built around that reads as a plain 2/2 without this.
            trigger["gain_deathrattle"] = True
        if trigger:
            trigger["limit"] = _limit_of(text)
            spec["on_friendly_death"] = trigger

    if "loses Divine Shield" in text:
        clause = text[text.find("loses Divine Shield") + len("loses Divine Shield"):]
        shield = _parse_payload(clause, index)
        gain = _parse_gain(clause)
        atk = RE_HAVE_ATK.search(clause)
        if gain:
            shield["gain"] = gain
        elif atk and "buff" not in shield:
            shield["buff"] = {"race": None, "attack": int(atk.group(1)), "health": 0,
                              "count": 0, "exclude_self": False}
        if shield:
            spec["on_ally_shield_lost"] = shield

    # "Rally" is the keyword the game now prints for "after this attacks" — the
    # log shows its trigger block nested inside the minion's own ATTACK block,
    # once per swing. Same hook, so the two share a clause.
    after_clause = _clause(text, "Rally:")
    clauses["after_attack"] = after_clause
    if not after_clause:
        match = re.search(r"(?:After|Whenever) this(?: minion)? attacks[^.]*", text, re.I)
        after_clause = match.group(0) if match else ""
    if after_clause:
        after = _parse_payload(after_clause, index)
        # Rally fires with a target in hand — the minion that was just hit — so
        # its payload has a referent nothing else in this vocabulary has.
        after.update(_parse_target(after_clause))
        gain = _parse_gain(after_clause)
        gem = RE_PLAYS_GEM_SELF.search(after_clause)
        if gem:
            # A Blood Gem is +1/+1, so N of them is a flat buff on itself.
            count = _num(gem.group(1))
            gain = {"attack": count, "health": count}
        if RE_GAIN_TIER_ATTACK.search(after_clause):
            after["gain_tier_attack"] = True
        if gain:
            after["gain"] = gain
        match = RE_DOUBLE_STATS.search(after_clause)
        if match:
            after["self_multiplier"] = 2 if match.group(1).lower() == "double" else 3
        if RE_DESTROY_SELF.search(after_clause):
            after["destroy_self"] = True
        if after:
            after["limit"] = _limit_of(after_clause) or _limit_of(text)
            spec["after_attack"] = after

    # "After a friendly Rally minion attacks, trigger your left-most Deathrattle."
    # Same hook as the generic ally-attack watcher below, but it fires *after*
    # the swing has resolved and only for allies carrying the Rally keyword.
    rally_watch = RE_RALLY_ALLY_ATTACK.search(text)
    clauses["ally_rally_attack"] = rally_watch.group(1) if rally_watch else ""
    if rally_watch:
        watcher = _parse_payload(rally_watch.group(1), index)
        gain = _parse_gain(rally_watch.group(1))
        if gain:
            watcher["gain"] = gain
        if watcher:
            spec["on_rally_attack"] = watcher

    # "Whenever another friendly minion attacks, this plays a Blood Gem on it."
    match = RE_ALLY_ATTACK.search(text)
    clauses["on_ally_attack"] = match.group(3) if match else ""
    if match:
        qualifier, subject, clause = (match.group(1) or ""), match.group(2), match.group(3)
        # Three targets share this trigger, and the wording is what separates
        # them: "give it"/"a Blood Gem on it" means the minion that swung,
        # a bare "gain" means the watcher, and anything naming minions ("your
        # Beasts", "all friendly minions") is a board buff.
        trigger: dict[str, Any] = _parse_payload(clause, index)
        gain = _parse_gain(clause)
        gem = RE_PLAYS_GEM_ON_IT.search(clause)
        if gem:
            # A Blood Gem is +1/+1, so N gems is a flat buff on the attacker.
            count = _num(gem.group(1))
            trigger.pop("buff", None)
            trigger["gain_attacker"] = {"attack": count, "health": count}
        elif RE_GIVE_TO_IT.search(clause):
            buff = trigger.pop("buff", None) or gain or {}
            trigger["gain_attacker"] = {"attack": int(buff.get("attack", 0)),
                                        "health": int(buff.get("health", 0))}
        elif gain:
            trigger["gain"] = gain
        if trigger:
            trigger["race"] = _race_of(subject)
            trigger["exclude_self"] = qualifier.strip().lower() in ("another",
                                                                   "a different")
            spec["on_ally_attack"] = trigger

    missing = _unmodelled(text, mechanics, spec, clauses)
    if missing:
        spec["unmodelled"] = missing
    return spec


# Wording that only ever pays off outside the fight: a deathrattle that hands
# you a card changes nothing about the combat in progress, so it must not count
# against the simulator's coverage.
RE_OFF_BOARD = re.compile(
    r"\b(?:Get |Add |to your hand|in your hand|in the Tavern|Tavern is Refreshed|"
    r"Tavern Coin|a Coin|Gold|Refresh|Discover|Cost|next turn|your teammate|"
    r"wherever they are|this game)\b", re.I)
RE_BOARD_ACTION = re.compile(
    r"\b(?:Summon|damage|Destroy|friendly|adjacent|your minions|your other|"
    r"this minion's|the minion that killed|left-most|right-most)\b", re.I)

# marker in the text -> spec key that should have come out of it. Start of
# Combat is missing on purpose: both snapshots are taken after the game has
# applied those effects, so an unparsed one costs the prediction nothing.
UNMODELLED_CHECKS = (
    ("deathrattle", r"Deathrattle:", "deathrattle"),
    ("avenge", r"Avenge \(\d+\)", "avenge"),
    ("frenzy", r"Frenzy:|first time this survives damage", "frenzy"),
    ("on_damaged", r"[Ww]henever this (?:minion )?takes damage", "on_damaged"),
    ("on_kill", r"(?:Whenever|After|When) this (?:minion )?(?:attacks and )?kills a minion",
     "on_kill"),
    ("on_friendly_death", rf"(?:Whenever|After) a(?:n| different| another)? "
                          rf"(?:friendly )?(?:Deathrattle )?{SUBJECT} dies",
     "on_friendly_death"),
    ("on_ally_attack", rf"(?:Whenever|After) (?:another |a different |a |an |your )?"
                       rf"friendly {SUBJECT} attacks", "on_ally_attack"),
    ("after_attack", r"Rally:|(?:Whenever|After) this(?: minion)? attacks", "after_attack"),
    ("on_summon", r"[Ww]henever you summon", "on_summon"),
    # "Once this reaches 6 Attack, gain Divine Shield" — nothing watches a stat
    # threshold mid-fight, so this one only gets to lower the confidence line
    # rather than quietly pass as a fully understood board.
    ("stat_threshold", r"Once this reaches \d+ Attack", "on_stat_threshold"),
    # Board-wide auras that multiply somebody else's trigger. Titus Rivendare
    # turns an Undead board into twice the bodies, and read as a vanilla 2/2 it
    # let the overlay call a lost fight at 100% *while reporting full coverage* —
    # the worst of both, a confident wrong answer with no warning on it.
    ("deathrattle_aura", r"[Yy]our Deathrattles trigger", "deathrattle_aura"),
    ("ally_rally_attack", r"After a friendly Rally minion attacks", "on_rally_attack"),
)


def _unmodelled(text: str, mechanics: set, spec: dict[str, Any],
                clauses: dict[str, str]) -> list[str]:
    """Which combat-relevant triggers this card has that we could not parse.

    Feeds the overlay's confidence line, so it deliberately ignores triggers
    whose whole payload happens in the tavern.
    """
    missing = []
    for name, marker, key in UNMODELLED_CHECKS:
        if spec.get(key):
            continue
        if not re.search(marker, text):
            continue
        if name in ("on_kill", "after_attack") and spec.get("cleave"):
            continue          # "deal excess damage to an adjacent enemy" is cleave
        clause = clauses.get(name) or text
        if RE_OFF_BOARD.search(clause) and not RE_BOARD_ACTION.search(clause):
            continue
        missing.append(name)
    if "DEATHRATTLE" in mechanics and "Deathrattle:" not in text \
            and not spec.get("deathrattle") and "deathrattle" not in missing:
        missing.append("deathrattle")
    # "Your Beetles have +5/+5 this game" is modelled from here on, but the
    # part that already accumulated over the previous ten turns is invisible:
    # no snapshot carries it, and it is usually the larger half. The engine
    # would otherwise report full coverage on exactly the board it understands
    # least — a Beetle warband whose tokens arrive at 200/200 in the real game
    # and at 60/60 here.
    if _has_game_buff(spec) and "game_carryover" not in missing:
        missing.append("game_carryover")
    return missing


def _has_game_buff(spec: Any) -> bool:
    """Whether anything in this card's spec grants a "this game" buff."""
    if isinstance(spec, dict):
        return "game_buff" in spec or any(_has_game_buff(v) for v in spec.values())
    if isinstance(spec, list):
        return any(_has_game_buff(v) for v in spec)
    return False


_DB: Optional[CardDB] = None


def get_db(locale: str = "enUS", offline: bool = False) -> CardDB:
    global _DB
    if _DB is None or _DB.locale != locale:
        _DB = CardDB(locale=locale, offline=offline)
        _DB.load()
    return _DB
