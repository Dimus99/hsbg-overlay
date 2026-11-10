"""Battlegrounds game state assembled from the Power.log event stream.

The tricky part is telling the opponent's combat board apart from Bob's tavern:
both live under the same "ghost" player id in ZONE=PLAY. We solve it by
capturing exactly the entities the game creates inside the combat-setup block.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Optional

from .logfiles import SCENE_GAMEPLAY
from .powerlog import Entity, Event, PowerLogParser

# Fallback ceiling for how long a finished fight stays on screen when the
# player never clicks anything. Overridable from settings.
COMBAT_HOLD_CAP_DEFAULT = 60.0

KEYWORD_TAGS = {
    "taunt": "TAUNT",
    "divine_shield": "DIVINE_SHIELD",
    "poisonous": "POISONOUS",
    "venomous": "VENOMOUS",
    "reborn": "REBORN",
    "windfury": "WINDFURY",
    "mega_windfury": "MEGA_WINDFURY",
    "stealth": "STEALTH",
    "frozen": "FROZEN",
    "cant_attack": "CANT_ATTACK",
}


@dataclass
class MinionSnapshot:
    entity_id: int = 0
    card_id: str = ""
    name: str = ""
    attack: int = 0
    health: int = 1
    tier: int = 1
    golden: bool = False
    position: int = 0
    races: frozenset[str] = frozenset()
    taunt: bool = False
    divine_shield: bool = False
    poisonous: bool = False
    venomous: bool = False
    reborn: bool = False
    windfury: bool = False
    mega_windfury: bool = False
    stealth: bool = False
    frozen: bool = False
    cant_attack: bool = False
    # raw script data, used by a few card effects (e.g. accumulated buffs)
    script_data: tuple[int, int, int] = (0, 0, 0)

    @property
    def base_card_id(self) -> str:
        return self.card_id[:-2] if self.card_id.endswith("_G") else self.card_id


@dataclass
class BoardSnapshot:
    player_id: int = 0
    player_name: str = ""
    hero_card_id: str = ""
    hero_name: str = ""
    hero_health: int = 0
    hero_armor: int = 0
    tech_level: int = 1
    turn: int = 0
    captured_at: float = 0.0
    minions: list[MinionSnapshot] = field(default_factory=list)
    # quest/trinket/hero-power ids that the simulator may care about
    hero_power_card_id: str = ""
    trinkets: tuple[str, ...] = ()
    # True when the game has already applied start-of-combat effects to these
    # stats, so the simulator must not apply them a second time.
    post_start_of_combat: bool = False
    # Minions held in hand. Filled for our own board only: several cards summon
    # out of hand mid-fight ("Rally: Summon the highest-Attack minion from your
    # hand"), and an opponent's hand never appears in the log.
    hand: list[MinionSnapshot] = field(default_factory=list)

    def copy(self) -> "BoardSnapshot":
        return replace(self, minions=[replace(m) for m in self.minions],
                       hand=[replace(m) for m in self.hand],
                       trinkets=tuple(self.trinkets))


@dataclass
class PlayerInfo:
    player_id: int = 0
    name: str = ""
    hero_card_id: str = ""
    hero_name: str = ""
    health: int = 0
    armor: int = 0
    damage: int = 0
    tech_level: int = 1
    place: int = 0
    dead: bool = False

    @property
    def effective_health(self) -> int:
        return max(0, self.health - self.damage) + self.armor


@dataclass
class CombatRecord:
    turn: int
    opponent_player_id: int
    my_board: BoardSnapshot
    opponent_board: BoardSnapshot
    started_at: float
    actual_result: str = ""     # win | loss | tie, filled in once damage lands
    actual_damage: int = 0
    # Hero damage the log itself reported during this fight (META_DATA). This
    # settles the result outright; the health-delta fallback below only runs
    # when the game never printed one.
    damage_to_me: int = 0
    damage_to_opponent: int = 0
    result_from_log: bool = False
    my_health_before: tuple[int, int] = (0, 0)      # (damage, armor)
    opp_health_before: tuple[int, int] = (0, 0)
    attacks: int = 0            # ATTACK blocks in this fight, for the history line
    damage_cap: int = 0         # the lobby's damage ceiling when this fight began
    # What the game actually did, swing by swing:
    #   ("attack", attacker_entity_id, defender_entity_id)
    #   ("death",  entity_id)
    #   ("summon", entity_id, card_id, controller_player_id)
    # Only filled when ``trace_combat`` is on — it is a few hundred tuples per
    # fight, which the live overlay has no use for. ``tools/divergence.py``
    # replays it against the simulator to find where the two stop agreeing,
    # which is the only way to tell an attack-order bug from a missing effect.
    trace: list = field(default_factory=list)
    # What the simulator said before the fight resolved, as a plain dict so it
    # can be written straight out. Filled by the app when it publishes odds;
    # ``None`` when the fight was never predicted (we were not watching).
    prediction: Optional[dict] = None


class BattlegroundsState:
    """Consumes parser events and keeps a live picture of the lobby."""

    def __init__(self, on_change: Optional[Callable[[str], None]] = None,
                 hold_cap: float = COMBAT_HOLD_CAP_DEFAULT) -> None:
        self.parser = PowerLogParser()
        self.hold_cap = hold_cap
        self.on_change = on_change or (lambda reason: None)
        # Record what the game did swing by swing (see CombatRecord.trace). Off
        # for the live overlay, which never reads it; on for tools/divergence.py.
        self.trace_combat = False
        # Called once per fight, the moment its real outcome is known. The debug
        # journal hangs off this; scoring is the only point where the prediction
        # and the result are both in hand.
        self.on_combat_scored: Optional[Callable[[CombatRecord], None]] = None
        # Survives reset() so a session's history spans several matches.
        self.archive: list[CombatRecord] = []
        # Which Hearthstone screen is up, fed from LoadingScreen.log. Belongs to
        # the client, not to any one match, so reset() must not clear it.
        self.scene = ""
        self.reset()

    # ------------------------------------------------------------------ setup

    def reset(self) -> None:
        self.is_battlegrounds = False
        self.game_active = False
        # The lobby has been decided (the log said STATE=COMPLETE). The player
        # may still be watching the last fight, so this only means "nothing new
        # is coming" — leaving the gameplay scene is what takes it off screen.
        self.game_over = False
        self.my_player_id = 0
        self.ghost_player_id = 0
        self.turn = 0
        # What the log says right now. The screen lags behind it — see
        # display_phase() — because the game dumps a whole combat at once and
        # only then plays the 10-30s animation.
        self.log_phase = "idle"           # idle | shop | combat
        self.damage_cap = 0               # 0 = no ceiling (also: none announced yet)
        self._damage_cap_raw = 0          # the announced ceiling, ignoring the switch
        self.damage_cap_enabled = True
        self.combat_resolved_at = 0.0
        self.combat_watched = False
        self.players: dict[int, PlayerInfo] = {}
        self.last_boards: dict[int, BoardSnapshot] = {}
        self.next_opponent_id = 0
        self.current_combat: Optional[CombatRecord] = None
        self.combat_history: list[CombatRecord] = []
        self.seen_cards: dict[str, int] = {}      # base card id -> max copies seen at once
        self.my_hand: list[MinionSnapshot] = []
        self._combat_setup = False
        self._combat_block_depth = 0
        self._combat_entity_ids: set[int] = set()
        self._combat_players: list[int] = []
        self._pending_names: dict[str, int] = {}
        # [attacker id, defender id] being filled in inside an ATTACK block.
        self._pending_swing: Optional[list] = None

    # ------------------------------------------------------------------ feed

    def feed_line(self, line: str) -> None:
        for event in self.parser.feed(line):
            self._handle(event)

    # ------------------------------------------------------------------ scene

    def set_scene(self, scene: str) -> None:
        """Which screen Hearthstone is showing, from LoadingScreen.log.

        Power.log cannot answer this: it keeps the finished match in memory long
        after the player is back in the menu picking their next game, and the
        combat readout then describes a lobby that no longer exists. The scene
        is the one honest "the game is over, you are looking at menus" signal —
        in a recorded match the log said COMPLETE at 05:33:06 while the player
        left the match scene 53 seconds later.
        """
        if not scene or scene == self.scene:
            return
        was_gameplay = self.in_gameplay
        self.scene = scene
        if was_gameplay and scene != SCENE_GAMEPLAY:
            self._end_game()
        self.on_change("scene")

    @property
    def in_gameplay(self) -> bool:
        """True while a match is on screen.

        An unknown scene counts as gameplay: LoadingScreen.log only starts being
        written after its log section is enabled and the game restarted, and
        blanking a working overlay because one log is missing would be worse
        than the staleness this guards against.
        """
        return self.scene in ("", SCENE_GAMEPLAY)

    def feed_lines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.feed_line(line)

    # --------------------------------------------------------------- handling

    def _handle(self, ev: Event) -> None:
        kind = ev.kind

        if kind == "create_game":
            # A new match starts: settle and file away the one that just ended.
            self.finalize_pending_result()
            self.archive.extend(self.combat_history)
            archive = self.archive
            self.reset()
            self.archive = archive
            self.game_active = True
            return

        if kind == "game_info" and ev.tag == "GameType":
            self.is_battlegrounds = "BATTLEGROUNDS" in ev.value
            self.on_change("game_type")
            return

        if kind == "player":
            # The account with a real GameAccountId is us; the zeroed one is the
            # "ghost" that owns Bob's tavern and every opposing combat board.
            if ev.account_hi != 0:
                self.my_player_id = ev.player_id
            else:
                self.ghost_player_id = ev.player_id
            return

        if kind == "player_name":
            info = self.players.setdefault(ev.player_id, PlayerInfo(player_id=ev.player_id))
            info.name = ev.name
            return

        if kind == "tag_unresolved":
            # Player-name references we could not map yet. Combat tags reveal the
            # name -> playerId mapping for every opponent we ever fight.
            if ev.tag == "BACON_CURRENT_COMBAT_PLAYER_ID" and ev.value not in ("0", ""):
                try:
                    pid = int(ev.value)
                except ValueError:
                    return
                name = ev.name
                if name and not name.startswith("["):
                    self._pending_names[name] = pid
                    info = self.players.setdefault(pid, PlayerInfo(player_id=pid))
                    if not info.name:
                        info.name = name
                self._begin_combat(pid)
            return

        if kind == "block_start":
            # The game applies every start-of-combat effect between the setup
            # block and the first attack, so the last honest moment to read both
            # boards is right before that first ATTACK block. Snapshotting there
            # means hero powers, trinkets and start-of-combat minions are already
            # baked into the stats and need no modelling at all.
            if self._combat_setup and ev.block_type == "ATTACK":
                self._finish_combat_setup()
            if (ev.block_type == "ATTACK" and self.current_combat is not None
                    and self.log_phase == "combat"):
                self.current_combat.attacks += 1
                # The pair of ids arrives as PROPOSED_ATTACKER / PROPOSED_DEFENDER
                # tags *inside* this block; the block header's own ``Target=`` is
                # always 0 in Battlegrounds.
                self._pending_swing = [0, 0] if self.trace_combat else None
            return

        if kind == "send_option":
            # A real click: the player is interacting with the tavern again, so
            # whatever combat we were holding on screen has finished animating.
            if self.current_combat is not None and self.log_phase != "combat":
                self.combat_watched = True
                self.on_change("combat_watched")
            return

        if kind == "meta_damage":
            if ev.entity is not None:
                self._note_hero_damage(ev.entity, _int(ev.value))
            return

        if kind == "block_end":
            # Both halves of a swing must arrive inside the same ATTACK block.
            # Left open, a PROPOSED_ATTACKER from one block paired with a
            # PROPOSED_DEFENDER from an unrelated later one and invented a swing.
            self._pending_swing = None
            return

        if kind in ("full_entity", "show_entity", "change_entity"):
            if self._combat_setup and ev.entity is not None:
                self._combat_entity_ids.add(ev.entity.id)
            return

        if kind == "tag" and ev.entity is not None:
            self._handle_tag(ev.entity, ev.tag, ev.value)

    def _sync_damage_cap(self) -> None:
        cap = self._damage_cap_raw if self.damage_cap_enabled else 0
        if cap != self.damage_cap:
            self.damage_cap = cap
            self.on_change("damage_cap")

    def _handle_tag(self, ent: Entity, tag: str, value: str) -> None:
        if tag == "TURN" and ent.id == self.parser.game_entity_id:
            try:
                self.turn = int(value)
            except ValueError:
                pass
            return

        if tag in ("PROPOSED_ATTACKER", "PROPOSED_DEFENDER"):
            # Who is swinging at whom, straight from the game. This is the whole
            # of tools/divergence.py's ground truth: with it the simulator can be
            # pinned to the real move list instead of guessing its own.
            swing = self._pending_swing
            if swing is not None:
                swing[0 if tag == "PROPOSED_ATTACKER" else 1] = _int(value)
                if swing[0] and swing[1] and self.current_combat is not None:
                    self.current_combat.trace.append(("attack", swing[0], swing[1]))
                    self._pending_swing = None
            return

        if tag == "STATE" and ent.id == self.parser.game_entity_id:
            if value == "COMPLETE":
                # The lobby is decided. Score the last fight now — no further
                # combat will arrive to settle it — but leave it on screen: the
                # player is still watching that fight animate.
                self.finalize_pending_result()
                if not self.game_over:
                    self.game_over = True
                    self.on_change("game_over")
            return

        if tag == "BACON_COMBAT_DAMAGE_CAP":
            # Battlegrounds clamps how much a lost fight can cost: 2 early,
            # rising to 5/10/15 as the lobby thins out. Without it the overlay
            # happily predicts 32 damage in a game that can only deal 15.
            cap = _int(value)
            if cap and cap != self._damage_cap_raw:
                self._damage_cap_raw = cap
                self._sync_damage_cap()
            return

        if tag == "BACON_COMBAT_DAMAGE_CAP_ENABLED":
            # The lobby lifts the ceiling once it thins out to the last few
            # players. Missing this pinned every late prediction at 15 while
            # real fights were landing 25-34.
            enabled = value not in ("0", "")
            if enabled != self.damage_cap_enabled:
                self.damage_cap_enabled = enabled
                self._sync_damage_cap()
            return

        if tag == "BACON_CURRENT_COMBAT_PLAYER_ID":
            pid = _int(value)
            if pid:
                self._begin_combat(pid)
            else:
                self._end_combat()
            return

        if tag == "NEXT_OPPONENT_PLAYER_ID":
            pid = _int(value)
            if pid and pid != self.next_opponent_id:
                self.next_opponent_id = pid
                self.on_change("next_opponent")
            return

        # Hero entities carry the whole leaderboard: one per player, tagged with
        # the player id they belong to.
        if ent.tag("CARDTYPE") == "HERO" and ent.int_tag("PLAYER_ID"):
            self._update_player_from_hero(ent)
            return

        if ent.tag("CARDTYPE") == "HERO" and ent.int_tag("CONTROLLER") == self.my_player_id:
            self._update_player_from_hero(ent, force_player_id=self.my_player_id)
            return

        if tag in ("ZONE", "ZONE_POSITION", "CONTROLLER", "ATK", "HEALTH", "DAMAGE"):
            if ent.tag("CARDTYPE") == "MINION":
                # Only past the setup block: until the first ATTACK every minion
                # on both boards is still being announced, and reading those as
                # mid-combat summons would fill the trace with the whole board.
                if (self.trace_combat and tag == "ZONE" and not self._combat_setup
                        and self.log_phase == "combat" and self.current_combat is not None):
                    if value == "GRAVEYARD":
                        self.current_combat.trace.append(("death", ent.id))
                    elif value == "PLAY":
                        self.current_combat.trace.append(
                            ("summon", ent.id, ent.card_id, ent.int_tag("CONTROLLER")))
                self.on_change("board")

    def _update_player_from_hero(self, ent: Entity, force_player_id: int = 0) -> None:
        pid = force_player_id or ent.int_tag("PLAYER_ID")
        if not pid:
            return
        info = self.players.setdefault(pid, PlayerInfo(player_id=pid))
        if ent.card_id and "HERO" in ent.card_id.upper():
            info.hero_card_id = ent.card_id
            info.hero_name = ent.name or info.hero_name
        info.health = ent.int_tag("HEALTH", info.health)
        info.armor = ent.int_tag("ARMOR", info.armor)
        info.damage = ent.int_tag("DAMAGE", info.damage)
        info.tech_level = ent.int_tag("PLAYER_TECH_LEVEL", info.tech_level)
        place = ent.int_tag("PLAYER_LEADERBOARD_PLACE")
        if place:
            info.place = place
        if info.health and info.damage >= info.health and info.armor <= 0:
            info.dead = True
        self.on_change("leaderboard")

    # ----------------------------------------------------------------- combat

    def _begin_combat(self, player_id: int) -> None:
        if not self._combat_setup:
            self._combat_setup = True
            self._combat_block_depth = self.parser.block_depth
            self._combat_entity_ids = set()
            self._combat_players = []
            self.log_phase = "combat"
        if player_id not in self._combat_players:
            self._combat_players.append(player_id)

    def _finish_combat_setup(self) -> None:
        # The previous combat's damage has landed by now — score it.
        self.finalize_pending_result()
        opponent_id = next((p for p in self._combat_players if p != self.my_player_id), 0)

        my_board = self._snapshot_my_board(post_start_of_combat=True)
        opp_board = self._snapshot_opponent_board(opponent_id)

        if opponent_id and opp_board.minions is not None:
            self.last_boards[opponent_id] = opp_board.copy()
        self._note_seen_minions(opp_board.minions)

        record = CombatRecord(
            turn=self.turn,
            opponent_player_id=opponent_id,
            my_board=my_board,
            opponent_board=opp_board,
            started_at=time.time(),
            damage_cap=self.damage_cap,
        )
        me = self.players.get(self.my_player_id)
        opp = self.players.get(opponent_id)
        record.my_health_before = (me.damage if me else 0, me.armor if me else 0)
        record.opp_health_before = (opp.damage if opp else 0, opp.armor if opp else 0)

        self.current_combat = record
        self.combat_resolved_at = 0.0
        self.combat_watched = False
        self.combat_history.append(record)
        # Cleared only now, with the new record in place: the scoring and pool
        # updates above fire on_change, and until this line those listeners
        # would read the *previous* fight as the current one.
        self._combat_setup = False
        self.on_change("combat_start")

    def _end_combat(self) -> None:
        if self.log_phase != "combat":
            return
        if self._combat_setup:
            # Nobody ever attacked (an empty board on one side): snapshot now,
            # otherwise this combat would never be recorded at all.
            self._finish_combat_setup()
        self.log_phase = "shop"
        # current_combat deliberately stays set: the player is still watching
        # the fight that this line already resolved.
        self.combat_resolved_at = time.time()
        self.on_change("combat_end")

    def _end_game(self) -> None:
        """The match has left the screen: drop everything that describes a live
        fight, so the overlay has nothing stale to draw over the menus.

        The history is deliberately kept — it is archived by the next
        CREATE_GAME, and doing it here as well would count every fight twice.
        """
        self.finalize_pending_result()
        self.game_active = False
        self.game_over = True
        self.log_phase = "idle"
        self.current_combat = None
        self.combat_watched = True
        self.combat_resolved_at = 0.0
        self.next_opponent_id = 0
        self.on_change("game_end")

    def _note_hero_damage(self, ent: Entity, amount: int) -> None:
        """Combat damage the game announced, split by whose hero took it."""
        record = self.current_combat
        if record is None or amount <= 0 or self.log_phase != "combat":
            return
        if ent.tag("CARDTYPE") != "HERO":
            return
        controller = ent.int_tag("CONTROLLER")
        if controller == self.my_player_id:
            record.damage_to_me += amount
        elif controller in (record.opponent_player_id, self.ghost_player_id):
            # The opponent fights us as a ghost copy, so their hero can arrive
            # under either id. Every *other* hero in the log belongs to somebody
            # else's fight in the same lobby — counting those inflated the
            # "actual" column past the lobby's damage cap.
            record.damage_to_opponent += amount

    def finalize_pending_result(self) -> None:
        """Score the previous combat once its damage has actually landed.

        Hero damage is applied *after* the combat block closes, so reading it at
        combat end would score almost everything as a tie. We settle the
        previous combat when the next one starts (or when the game ends).
        """
        for record in reversed(self.combat_history):
            if record.actual_result:
                break
            self._record_actual_result(record)

    def _record_actual_result(self, record: CombatRecord) -> None:
        """What really happened, so the overlay can show predicted vs actual."""
        self._score_actual_result(record)
        hook = self.on_combat_scored
        if hook is not None and record.actual_result:
            try:
                hook(record)
            except Exception:
                # A diagnostic must never be able to break a live match.
                pass

    def _score_actual_result(self, record: CombatRecord) -> None:
        if record.damage_to_me or record.damage_to_opponent:
            # The game said it outright — no inference needed. Only one side
            # can be damaged by a fight, so the larger number decides.
            record.result_from_log = True
            if record.damage_to_me >= record.damage_to_opponent:
                record.actual_result = "loss"
                record.actual_damage = record.damage_to_me
            else:
                record.actual_result = "win"
                record.actual_damage = record.damage_to_opponent
            return

        me = self.players.get(self.my_player_id)
        opp = self.players.get(record.opponent_player_id)
        my_loss = _health_delta(record.my_health_before, me)
        opp_loss = _health_delta(record.opp_health_before, opp)
        # Inferring from the health bars picks up armour the hero was *granted*
        # as well as damage taken — 17 "damage" on turn 4 in one recorded game.
        # A fight can never exceed the lobby's ceiling, so clamp to it.
        if record.damage_cap:
            my_loss = min(my_loss, record.damage_cap)
            opp_loss = min(opp_loss, record.damage_cap)
        if my_loss > 0:
            record.actual_result, record.actual_damage = "loss", my_loss
        elif opp_loss > 0:
            record.actual_result, record.actual_damage = "win", opp_loss
        else:
            record.actual_result, record.actual_damage = "tie", 0

    # ---------------------------------------------------------------- boards

    def _live_minions(self, controller: int, only_ids: Optional[set[int]] = None
                      ) -> list[MinionSnapshot]:
        out: list[MinionSnapshot] = []
        for ent in self.parser.entities.values():
            if only_ids is not None and ent.id not in only_ids:
                continue
            if ent.tag("CARDTYPE") != "MINION":
                continue
            if ent.tag("ZONE") != "PLAY":
                continue
            if ent.int_tag("CONTROLLER") != controller:
                continue
            pos = ent.int_tag("ZONE_POSITION")
            if pos < 1 or pos > 7:
                continue
            out.append(minion_from_entity(ent))
        out.sort(key=lambda m: m.position)
        return out

    # The two picker cards that stand in for an unchosen slot. They are trinkets
    # by card type but describe nothing, so they must not reach the simulator.
    TRINKET_PLACEHOLDERS = ("BG30_Trinket_1st", "BG30_Trinket_2nd")

    def _trinkets(self, controller: int = 0,
                  only_ids: Optional[set[int]] = None) -> tuple[str, ...]:
        """Equipped trinket card ids for one side.

        Ours are found by controller; an opponent's arrive inside the
        combat-setup block under a fresh proxy controller that differs every
        fight, so there the block's entity ids are the filter instead. Both sit
        in ZONE=PLAY — the discarded offers stay in REMOVEDFROMGAME.
        """
        out = []
        for ent in self.parser.entities.values():
            if only_ids is not None and ent.id not in only_ids:
                continue
            if ent.tag("CARDTYPE") != "BATTLEGROUND_TRINKET":
                continue
            if ent.tag("ZONE") != "PLAY" or not ent.card_id:
                continue
            if controller and ent.int_tag("CONTROLLER") != controller:
                continue
            if ent.card_id in self.TRINKET_PLACEHOLDERS:
                continue
            out.append((ent.int_tag("ZONE_POSITION"), ent.card_id))
        return tuple(card_id for _, card_id in sorted(out))

    def _hand_minions(self) -> list[MinionSnapshot]:
        """Minions in our hand, for the cards that summon out of it in combat."""
        out: list[MinionSnapshot] = []
        for ent in self.parser.entities.values():
            if ent.tag("CARDTYPE") != "MINION" or ent.tag("ZONE") != "HAND":
                continue
            if ent.int_tag("CONTROLLER") != self.my_player_id:
                continue
            out.append(minion_from_entity(ent))
        return out

    def _snapshot_my_board(self, post_start_of_combat: bool = False) -> BoardSnapshot:
        info = self.players.get(self.my_player_id, PlayerInfo(player_id=self.my_player_id))
        return BoardSnapshot(
            player_id=self.my_player_id,
            player_name=info.name,
            hero_card_id=info.hero_card_id,
            hero_name=info.hero_name,
            hero_health=info.health,
            hero_armor=info.armor,
            tech_level=info.tech_level,
            turn=self.turn,
            captured_at=time.time(),
            post_start_of_combat=post_start_of_combat,
            minions=self._live_minions(self.my_player_id),
            hand=self._hand_minions(),
            trinkets=self._trinkets(controller=self.my_player_id),
        )

    def _snapshot_opponent_board(self, opponent_id: int) -> BoardSnapshot:
        info = self.players.get(opponent_id, PlayerInfo(player_id=opponent_id))
        # Only entities created inside the combat-setup block: that excludes the
        # tavern minions, which share the ghost controller.
        minions = self._live_minions(self.ghost_player_id, only_ids=self._combat_entity_ids)
        return BoardSnapshot(
            player_id=opponent_id,
            player_name=info.name,
            hero_card_id=info.hero_card_id,
            hero_name=info.hero_name,
            hero_health=info.health,
            hero_armor=info.armor,
            tech_level=info.tech_level,
            turn=self.turn,
            captured_at=time.time(),
            post_start_of_combat=True,
            minions=minions,
            trinkets=self._trinkets(only_ids=self._combat_entity_ids),
        )

    def display_phase(self, now: Optional[float] = None) -> str:
        """What the player is looking at, which is not what the log just said.

        Hearthstone dumps a whole combat into the log in well under a second and
        only then animates it. Measured across nine fights in one match: the log
        burst finished 0.3-1.8s in, while the player's first actual click came
        20-84s later. Every in-log marker that might mean "combat over" — the
        tavern returning, the turn counter ticking, our board changing — fires
        inside that burst, so none of them can be used, and guessing the
        animation length from the attack count cut the panel off mid-fight.

        So the end signal is the player themselves: the odds stay up until the
        first thing they click in the tavern.
        """
        record = self.current_combat
        if record is None:
            return self.log_phase if self.log_phase != "combat" else "combat"
        if self.log_phase == "combat":
            return "combat"
        if self.combat_watched:
            return "shop"
        now = now if now is not None else time.time()
        if now - self.combat_resolved_at > self.hold_cap:
            return "shop"
        return "combat"

    @property
    def phase(self) -> str:
        return self.display_phase()

    @property
    def combat_pending(self) -> bool:
        """A new fight is being assembled and cannot be read yet.

        Between the combat marker and the first ATTACK the log is still
        creating the opposing board, while ``current_combat`` — and the phase —
        already say "combat". Anything computed from that record in this window
        describes the *previous* fight, so callers must wait it out instead.
        """
        return self._combat_setup

    def current_my_board(self) -> BoardSnapshot:
        """Live board during the shop phase, for pre-combat predictions."""
        return self._snapshot_my_board()

    @property
    def display_turn(self) -> int:
        """The turn number the game shows.

        The TURN tag ticks once for the tavern and once for the combat, so it
        runs at twice the rate of the number on screen.
        """
        return max(1, (self.turn + 1) // 2)

    def hero_choices(self) -> list[str]:
        """Card ids of the heroes currently offered to us, left to right.

        During the pick they sit in our hand; once one is chosen the rest
        leave, so this empties itself.
        """
        out = []
        for ent in self.parser.entities.values():
            if ent.tag("CARDTYPE") != "HERO" or ent.tag("ZONE") != "HAND":
                continue
            if ent.int_tag("CONTROLLER") != self.my_player_id or not ent.card_id:
                continue
            out.append((ent.int_tag("ZONE_POSITION"), ent.card_id))
        return [card_id for _, card_id in sorted(out)]

    def current_tavern(self) -> list[MinionSnapshot]:
        """What Bob is offering right now, left to right.

        Outside combat the ghost player owns exactly the tavern row, so no
        filtering by creation block is needed here.
        """
        if self.display_phase() == "combat" or not self.ghost_player_id:
            return []
        return self._live_minions(self.ghost_player_id)

    # ------------------------------------------------------------------ pool

    def _note_seen_minions(self, minions: Iterable[MinionSnapshot]) -> None:
        counts: dict[str, int] = {}
        for m in minions:
            # A golden minion consumed three copies from the pool.
            counts[m.base_card_id] = counts.get(m.base_card_id, 0) + (3 if m.golden else 1)
        for card, n in counts.items():
            if n > self.seen_cards.get(card, 0):
                self.seen_cards[card] = n
        if counts:
            self.on_change("pool")

    # ----------------------------------------------------------------- views

    def leaderboard(self) -> list[PlayerInfo]:
        alive = [p for p in self.players.values() if p.player_id not in (self.ghost_player_id,)]
        return sorted(alive, key=lambda p: (p.place or 99, -p.effective_health))

    def opponent_boards(self) -> list[BoardSnapshot]:
        boards = [b for b in self.last_boards.values() if b.player_id != self.my_player_id]
        return sorted(boards, key=lambda b: -b.turn)


def _health_delta(before: tuple[int, int], player: Optional[PlayerInfo]) -> int:
    """Health actually lost between two points in time (armor counts too)."""
    if player is None:
        return 0
    prev_damage, prev_armor = before
    return max(0, (player.damage - prev_damage) + (prev_armor - player.armor))


def _int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def minion_from_entity(ent: Entity) -> MinionSnapshot:
    health = ent.int_tag("HEALTH", 1) - ent.int_tag("DAMAGE", 0)
    race = ent.tag("CARDRACE")
    races = frozenset([race]) if race else frozenset()
    snap = MinionSnapshot(
        entity_id=ent.id,
        card_id=ent.card_id,
        name=ent.name,
        attack=ent.int_tag("ATK", 0),
        health=max(0, health),
        tier=ent.int_tag("TECH_LEVEL", 1),
        golden=ent.tag("PREMIUM") == "1" or ent.card_id.endswith("_G"),
        position=ent.int_tag("ZONE_POSITION", 0),
        races=races,
        script_data=(ent.int_tag("TAG_SCRIPT_DATA_NUM_1"),
                     ent.int_tag("TAG_SCRIPT_DATA_NUM_2"),
                     ent.int_tag("TAG_SCRIPT_DATA_NUM_3")),
    )
    for attr, tag in KEYWORD_TAGS.items():
        setattr(snap, attr, ent.has(tag))
    # CANT_ATTACK is set while a card sits in hand or set-aside and the game
    # never clears it once the minion is played — reading it as "this minion
    # cannot attack" silently benched most of our own board. Nothing in
    # Battlegrounds actually prints "Can't attack", so in play it means nothing.
    if ent.tag("ZONE") == "PLAY":
        snap.cant_attack = False
    return snap
