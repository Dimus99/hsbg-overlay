"""Streaming tokenizer for Hearthstone's Power.log.

Turns raw ``GameState.DebugPrintPower()`` lines into a flat event stream and
maintains the entity table those events refer to. Everything Battlegrounds
specific lives one level up, in :mod:`hsbg.gamestate`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional

# ``D 23:25:44.9082660 GameState.DebugPrintPower() -     TAG_CHANGE ...``
RE_LINE = re.compile(r"^[DWE] (\d\d:\d\d:\d\d\.\d+) (\S+?)\(\) - (.*)$")

RE_ENTITY_DESC = re.compile(
    r"\[entityName=(?P<name>.*?) id=(?P<id>\d+) zone=(?P<zone>\S+) "
    r"zonePos=(?P<pos>-?\d+) cardId=(?P<card>\S*) player=(?P<player>\d+)\]"
)

RE_CREATE_GAME = re.compile(r"^\s*CREATE_GAME\s*$")
RE_GAME_ENTITY = re.compile(r"^\s*GameEntity EntityID=(\d+)\s*$")
RE_PLAYER = re.compile(r"^\s*Player EntityID=(\d+) PlayerID=(\d+) GameAccountId=\[hi=(\d+) lo=(\d+)\]")
RE_FULL_CREATE = re.compile(r"^\s*FULL_ENTITY - Creating ID=(\d+) CardID=(\S*)\s*$")
RE_FULL_UPDATE = re.compile(r"^\s*FULL_ENTITY - Updating (.+?) CardID=(\S*)\s*$")
RE_SHOW_ENTITY = re.compile(r"^\s*SHOW_ENTITY - Updating Entity=(.+?) CardID=(\S*)\s*$")
RE_CHANGE_ENTITY = re.compile(r"^\s*CHANGE_ENTITY - Updating Entity=(.+?) CardID=(\S*)\s*$")
RE_HIDE_ENTITY = re.compile(r"^\s*HIDE_ENTITY - Entity=(.+?) tag=(\S+) value=(\S+)\s*$")
RE_TAG = re.compile(r"^(\s*)tag=(\S+) value=(\S+)\s*$")
RE_TAG_CHANGE = re.compile(r"^\s*TAG_CHANGE Entity=(.+?) tag=(\S+) value=(\S+?)\s*(?:DEF CHANGE)?\s*$")
RE_BLOCK_START = re.compile(r"^\s*BLOCK_START BlockType=(\S+) Entity=(.+?) EffectCardId=")
RE_BLOCK_END = re.compile(r"^\s*BLOCK_END\s*$")
# The game's own damage bookkeeping: one META_DATA header naming the amount,
# then one Info line per entity that took it. This is the only place the log
# states a combat's outcome outright — every other signal has to be inferred.
RE_META = re.compile(r"^\s*META_DATA - Meta=(\S+) Data=(\d+)")
RE_META_INFO = re.compile(r"^\s*Info\[\d+\] = (.+?)\s*$")

RE_DEBUG_GAME = re.compile(r"^(\w+)=(.*)$")
RE_DEBUG_PLAYER = re.compile(r"^PlayerID=(\d+), PlayerName=(.*)$")
# "GameState.DebugPrintEntityChoices() - id=2 Player=X TaskList= ChoiceType=GENERAL …"
# opens a choice, "GameState.SendChoices() - id=2 ChoiceType=GENERAL" closes it.
# Only the header line carries the id; the Entities[n]= lines under it do not.
RE_CHOICE = re.compile(r"^id=(\d+)\b.*\bChoiceType=(\S+)")


@dataclass
class Entity:
    id: int
    card_id: str = ""
    name: str = ""
    tags: dict[str, str] = field(default_factory=dict)

    def tag(self, key: str, default: str = "") -> str:
        return self.tags.get(key, default)

    def int_tag(self, key: str, default: int = 0) -> int:
        raw = self.tags.get(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    def has(self, key: str) -> bool:
        return self.tags.get(key) not in (None, "0")


# --- events -----------------------------------------------------------------

@dataclass
class Event:
    kind: str
    entity: Optional[Entity] = None
    tag: str = ""
    value: str = ""
    player_id: int = 0
    account_hi: int = 0
    name: str = ""
    block_type: str = ""
    raw: str = ""


class PowerLogParser:
    """Feed it lines, get events. Holds the authoritative entity table."""

    def __init__(self) -> None:
        self.entities: dict[int, Entity] = {}
        self.game_entity_id: int = 0
        self.player_names: dict[int, str] = {}      # playerId -> account name
        self.name_to_player: dict[str, int] = {}
        self.game_type: str = ""
        self.build_number: str = ""
        self.block_depth: int = 0
        # entity whose indented ``tag=`` lines we are currently consuming
        self._cursor: Optional[Entity] = None
        self._cursor_indent: int = -1
        # amount from the META_DATA header whose Info lines follow, if any
        self._meta_damage: Optional[int] = None

    # -- helpers ------------------------------------------------------------

    def entity(self, eid: int) -> Entity:
        ent = self.entities.get(eid)
        if ent is None:
            ent = Entity(id=eid)
            self.entities[eid] = ent
        return ent

    def _resolve(self, ref: str) -> Optional[Entity]:
        """Entity references appear as a bracketed descriptor, a bare id, the
        literal ``GameEntity``, or a player's account name."""
        ref = ref.strip()
        desc = RE_ENTITY_DESC.search(ref)
        if desc:
            ent = self.entity(int(desc.group("id")))
            if desc.group("card"):
                ent.card_id = desc.group("card")
            if desc.group("name"):
                ent.name = desc.group("name")
            # A descriptor shows the state *before* the change on that line, and
            # for combat proxies it lags behind the real controller — so it only
            # fills in blanks, never overwrites what an explicit tag told us.
            ent.tags.setdefault("CONTROLLER", desc.group("player"))
            return ent
        if ref.isdigit():
            return self.entity(int(ref))
        if ref == "GameEntity":
            return self.entity(self.game_entity_id) if self.game_entity_id else None
        pid = self.name_to_player.get(ref)
        if pid is not None:
            for ent in self.entities.values():
                if ent.tag("CARDTYPE") == "PLAYER" and ent.int_tag("PLAYER_ID") == pid:
                    return ent
        return None

    # -- main entry point ---------------------------------------------------

    def feed(self, line: str) -> list[Event]:
        # Roughly half of Power.log is PowerTaskList output we never use, and the
        # file runs to tens of millions of lines. A substring test is an order of
        # magnitude cheaper than the regex, so it guards the hot path.
        # "GameState.Send" covers both SendOption and SendChoices, so the guard
        # stays at two substring tests.
        if "GameState.Debug" not in line and "GameState.Send" not in line:
            return []
        m = RE_LINE.match(line)
        if not m:
            return []
        source, body = m.group(2), m.group(3)

        if source == "GameState.DebugPrintGame":
            return self._feed_debug_game(body)
        if source == "GameState.DebugPrintEntityChoices":
            m2 = RE_CHOICE.match(body)
            return [Event("choice_open", value=m2.group(2))] if m2 else []
        if source == "GameState.SendChoices":
            m2 = RE_CHOICE.match(body)
            return [Event("choice_close", value=m2.group(2))] if m2 else []
        if source == "GameState.SendOption":
            # The player clicked something. Everything else in the log is sent
            # ahead of the screen, so this is our only real-time heartbeat.
            return [Event("send_option", raw=body)]
        if source != "GameState.DebugPrintPower":
            return []
        return self._feed_power(body)

    def _feed_debug_game(self, body: str) -> list[Event]:
        body = body.strip()
        mp = RE_DEBUG_PLAYER.match(body)
        if mp:
            pid, name = int(mp.group(1)), mp.group(2).strip()
            self.player_names[pid] = name
            self.name_to_player[name] = pid
            return [Event("player_name", player_id=pid, name=name)]
        mg = RE_DEBUG_GAME.match(body)
        if mg:
            key, value = mg.group(1), mg.group(2).strip()
            if key == "GameType":
                self.game_type = value
            elif key == "BuildNumber":
                self.build_number = value
            return [Event("game_info", tag=key, value=value)]
        return []

    def _feed_power(self, body: str) -> list[Event]:
        # 1. Indented ``tag=`` lines belong to whatever entity we last opened.
        mt = RE_TAG.match(body)
        if mt and self._cursor is not None and len(mt.group(1)) > self._cursor_indent:
            self._cursor.tags[mt.group(2)] = mt.group(3)
            return [Event("tag", entity=self._cursor, tag=mt.group(2), value=mt.group(3))]

        # 2. META_DATA and its Info lines: a header, then its subjects.
        mm = RE_META.match(body)
        if mm:
            self._cursor = None
            self._cursor_indent = -1
            self._meta_damage = int(mm.group(2)) if mm.group(1) == "DAMAGE" else None
            return []
        if self._meta_damage is not None:
            mi = RE_META_INFO.match(body)
            if mi:
                ent = self._resolve(mi.group(1))
                return ([Event("meta_damage", entity=ent, value=str(self._meta_damage))]
                        if ent is not None else [])
            self._meta_damage = None

        # Anything that is not an indented tag closes the current cursor.
        if not mt:
            self._cursor = None
            self._cursor_indent = -1

        if RE_CREATE_GAME.match(body):
            self.entities.clear()
            self.player_names.clear()
            self.name_to_player.clear()
            self.block_depth = 0
            return [Event("create_game")]

        mg = RE_GAME_ENTITY.match(body)
        if mg:
            self.game_entity_id = int(mg.group(1))
            ent = self.entity(self.game_entity_id)
            ent.name = "GameEntity"
            self._open(ent, body)
            return [Event("game_entity", entity=ent)]

        mp = RE_PLAYER.match(body)
        if mp:
            ent = self.entity(int(mp.group(1)))
            pid, hi = int(mp.group(2)), int(mp.group(3))
            ent.tags["PLAYER_ID"] = str(pid)
            ent.tags["CARDTYPE"] = "PLAYER"
            self._open(ent, body)
            return [Event("player", entity=ent, player_id=pid, account_hi=hi)]

        mc = RE_FULL_CREATE.match(body)
        if mc:
            ent = self.entity(int(mc.group(1)))
            if mc.group(2):
                ent.card_id = mc.group(2)
            self._open(ent, body)
            return [Event("full_entity", entity=ent)]

        for regex, kind in ((RE_FULL_UPDATE, "full_entity"),
                            (RE_SHOW_ENTITY, "show_entity"),
                            (RE_CHANGE_ENTITY, "change_entity")):
            mu = regex.match(body)
            if mu:
                ent = self._resolve(mu.group(1))
                if ent is None:
                    return []
                if mu.group(2):
                    ent.card_id = mu.group(2)
                self._open(ent, body)
                return [Event(kind, entity=ent)]

        mh = RE_HIDE_ENTITY.match(body)
        if mh:
            ent = self._resolve(mh.group(1))
            if ent is not None:
                ent.tags[mh.group(2)] = mh.group(3)
                return [Event("tag", entity=ent, tag=mh.group(2), value=mh.group(3))]
            return []

        mtc = RE_TAG_CHANGE.match(body)
        if mtc:
            ref, tag, value = mtc.group(1), mtc.group(2), mtc.group(3)
            ent = self._resolve(ref)
            if ent is not None:
                ent.tags[tag] = value
                return [Event("tag", entity=ent, tag=tag, value=value, name=ref)]
            # Player-name references before we learned the mapping still matter:
            # BACON_CURRENT_COMBAT_PLAYER_ID is how we learn who is who.
            return [Event("tag_unresolved", tag=tag, value=value, name=ref.strip())]

        mb = RE_BLOCK_START.match(body)
        if mb:
            self.block_depth += 1
            ent = self._resolve(mb.group(2))
            return [Event("block_start", entity=ent, block_type=mb.group(1),
                          value=str(self.block_depth))]

        if RE_BLOCK_END.match(body):
            depth = self.block_depth
            self.block_depth = max(0, self.block_depth - 1)
            return [Event("block_end", value=str(depth))]

        return []

    def _open(self, ent: Entity, body: str) -> None:
        self._cursor = ent
        self._cursor_indent = len(body) - len(body.lstrip())

    def feed_all(self, lines: Iterable[str]) -> Iterator[Event]:
        for line in lines:
            yield from self.feed(line)
