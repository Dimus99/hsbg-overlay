"""Wiring: log thread -> game state -> simulation thread -> overlay."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

from . import pool as pool_module
from .cardart import ArtCache
from .carddb import CardDB
from .config import Settings, hearthstone_is_fullscreen
from .debuglog import DebugJournal
from .gamestate import BattlegroundsState, BoardSnapshot
from .i18n import strings
from .logfiles import (PowerLogTailer, ensure_log_config, newest_log_dir,
                       parse_scene)
from .sim.runner import SimResult, Simulator, coverage_of
from .sim.model import board_from_snapshot
from .stats import ExternalStats, PersonalStats, scan_history
from .viewmodel import (HeroChoiceView, MinionView, OddsView, OpponentView,
                        PoolEntry, PopupView, StatLine, ViewModel)

DEBOUNCE = 0.30      # seconds to coalesce bursts of log activity


class App:
    def __init__(self, settings: Settings, overlay=None):
        self.settings = settings
        self.overlay = overlay
        self.language = settings.resolved_language()
        # Our own labels follow the game's locale, the same way card names do.
        self.t = strings(self.language)
        self.db = CardDB(locale=self.language, offline=settings.offline)
        self.art = ArtCache(locale=self.language, offline=settings.offline)
        self.state = BattlegroundsState(on_change=self._on_change,
                                        hold_cap=settings.combat_hold_seconds)
        self.simulator: Optional[Simulator] = None
        self.personal: PersonalStats = PersonalStats()
        self.external = ExternalStats(url=str(settings.extra.get("stats_url", "")),
                                      ttl_hours=settings.stats_refresh_hours,
                                      offline=settings.offline)
        self.warnings: list[str] = []
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._last_sim_key: tuple = ()
        self._last_result: Optional[tuple[SimResult, OddsView]] = None
        self._lock = threading.Lock()
        self._card_names: set[str] = set()
        self.journal = DebugJournal()
        # True while the log reader is still working through what was already
        # in Power.log when we started. Those fights are history — they were
        # never predicted, and journaling them writes the same match a second
        # time on every restart, with rows that ruin the calibration table.
        self.replaying = True
        # Scoring is the only moment the prediction and the real outcome are
        # both in hand, so that is where the journal row is written.
        self.state.on_combat_scored = self._on_combat_scored

    # ------------------------------------------------------------------ boot

    def prepare(self) -> None:
        changed, message = ensure_log_config()
        if changed:
            self.warnings.append(self.t(message))
        if hearthstone_is_fullscreen():
            self.warnings.append(self.t("warn.fullscreen"))
        if newest_log_dir() is None:
            self.warnings.append(self.t("warn.no_logs"))

        if not self.db.load():
            self.warnings.append(self.t("warn.card_db", error=self.db.error))
        self._card_names = {
            (card.get("name") or "").lower()
            for card in self.db.by_id.values() if card.get("name")
        }
        self.art.start(on_ready=lambda card_id: self._wake.set())
        spec = self.db.derive_effects() if self.db.loaded else {}
        # The token pool is what answers "summon a random Beast". Left out, every
        # such deathrattle summoned nothing at all in the live overlay, while the
        # measuring tools — which build their simulator elsewhere — had it all
        # along, so the bug never showed up in an accuracy run.
        pool = self.db.derive_pool() if self.db.loaded else []
        self.simulator = Simulator(effect_spec=spec, workers=self.settings.sim_workers,
                                   token_pool=pool)
        self._warm_up()

    def _warm_up(self) -> None:
        """Spin the worker pool up now, so the first real prediction is fast
        instead of paying ~1.5s of process startup mid-combat."""
        from .sim.model import SimBoard, SimMinion
        board = SimBoard(tier=1)
        board.minions.append(SimMinion(card_id="warmup", attack=1, health=1))
        try:
            self.simulator.run(board, board.clone(), iterations=8)
        except Exception:
            pass

    def start(self) -> None:
        threading.Thread(target=self._log_loop, name="hsbg-log", daemon=True).start()
        threading.Thread(target=self._scene_loop, name="hsbg-scene", daemon=True).start()
        threading.Thread(target=self._sim_loop, name="hsbg-sim", daemon=True).start()
        threading.Thread(target=self._history_loop, name="hsbg-history",
                         daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self.simulator is not None:
            self.simulator.shutdown()

    # --------------------------------------------------------------- threads

    def _log_loop(self) -> None:
        tailer = PowerLogTailer(from_start=True)
        for line in tailer.lines(stop=self._stop.is_set):
            # The first line that arrives after the backlog is drained is the
            # first one the game is writing *now*.
            self.replaying = not tailer.caught_up
            try:
                with self._lock:         # the scene reader writes state too
                    self.state.feed_line(line)
            except Exception:            # never let one odd line kill the reader
                continue

    def _scene_loop(self) -> None:
        """Follow the screen the player is on, which Power.log never says."""
        tailer = PowerLogTailer(from_start=True, filename="LoadingScreen.log",
                                start_marker=None)
        for line in tailer.lines(stop=self._stop.is_set):
            scene = parse_scene(line)
            if scene is None:
                continue
            try:
                with self._lock:
                    self.state.set_scene(scene)
            except Exception:
                continue

    def _history_loop(self) -> None:
        """Personal stats come from a full log scan — slow, so do it once, late."""
        try:
            self.personal = scan_history()
        except Exception:
            self.personal = PersonalStats()
        try:
            self.external.load()
        except Exception:
            pass
        self._wake.set()

    def _sim_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=1.0)
            if self._stop.is_set():
                break
            self._wake.clear()
            time.sleep(DEBOUNCE)          # coalesce the burst that follows a change
            try:
                self._recompute()
            except Exception:
                continue

    def _on_change(self, reason: str) -> None:
        self._wake.set()

    # ------------------------------------------------------------- computing

    def _matchup(self):
        """The fight to simulate as ``(record, my_board, opponent_board)``.

        The record travels with the boards on purpose. The simulation runs on
        this thread for a second or more while the log reader keeps going, so
        by the time it finishes ``state.current_combat`` may already be a
        *later* fight — and pinning the answer to whatever is current then
        files a prediction against a fight it does not describe. Replaying a
        match from the top (the overlay reads the log from the last
        CREATE_GAME) makes that the normal case rather than a rare race: the
        journal showed a fight called at 0% that was won, purely because the
        numbers belonged to a different turn.
        """
        combat = self.state.current_combat
        if self.state.combat_pending:
            # The next fight is mid-assembly; ``combat`` still holds the last
            # one, and simulating that would publish a wrong prediction for a
            # second or two before the real one lands.
            return None, None, None
        if combat is not None and self.state.display_phase() == "combat":
            return combat, combat.my_board, combat.opponent_board
        return None, None, None

    def _recompute(self) -> None:
        if self.state.combat_pending:
            self._publish(self._build_model(self._pending_odds(), pending=True))
            return

        record, my_board, opponent_board = self._matchup()
        odds: Optional[OddsView] = None

        if opponent_board is not None and (my_board.minions or opponent_board.minions):
            key = (tuple((m.card_id, m.attack, m.health, m.taunt, m.divine_shield,
                          m.poisonous, m.venomous, m.reborn, m.windfury)
                         for m in my_board.minions),
                   tuple((m.card_id, m.attack, m.health, m.taunt, m.divine_shield,
                          m.poisonous, m.venomous, m.reborn, m.windfury)
                         for m in opponent_board.minions))
            if key == self._last_sim_key and self._last_result is not None:
                odds = self._last_result[1]
                # Same boards, but possibly a different fight: the cached answer
                # still has to be filed against the record in hand, or that
                # fight reaches the journal with no prediction at all.
                self._remember_prediction(record, odds)
            else:
                # The run below blocks for up to the time budget. Show the
                # loader for that stretch rather than leaving stale numbers up.
                self._publish(self._build_model(self._pending_odds(opponent_board),
                                                pending=True))
                odds = self._simulate(record, my_board, opponent_board)
                self._last_sim_key = key

        self._publish(self._build_model(odds))

    def _publish(self, model: ViewModel) -> None:
        self.last_model = model
        if self.overlay is not None:
            # Hover mapping needs to know how many cards Bob is showing.
            self.overlay.set_tavern_slots(len(self.state.current_tavern()))
            self.overlay.update(model)

    def _pending_odds(self, opponent_board: Optional[BoardSnapshot] = None) -> OddsView:
        """Placeholder carrying the one thing we already know: who we fight."""
        if opponent_board is not None:
            name = self._player_label(opponent_board.player_name,
                                      opponent_board.hero_card_id,
                                      opponent_board.player_id)
        else:
            info = self.state.players.get(self.state.next_opponent_id)
            name = self._player_label(info.name if info else "",
                                      info.hero_card_id if info else "",
                                      self.state.next_opponent_id)
        return OddsView(headline=self.t("odds.vs", name=name) if name and name != "?"
                                 else self.t("odds.combat"),
                        subtitle=self.t("odds.pending"), known=False)

    def _simulate(self, record, my_board: BoardSnapshot,
                  opponent_board: BoardSnapshot) -> OddsView:
        assert self.simulator is not None
        powers, trinkets = self.db.hero_power_specs, self.db.trinket_specs
        board_a = board_from_snapshot(my_board, powers, trinkets)
        board_b = board_from_snapshot(opponent_board, powers, trinkets)
        result = self.simulator.run(board_a, board_b,
                                    iterations=self.settings.iterations,
                                    damage_cap=self.state.damage_cap,
                                    time_budget=self.settings.sim_time_budget)
        coverage, unknown = coverage_of(board_a, board_b)

        effective_health = sum(self._my_life(record))

        opponent_name = self._player_label(opponent_board.player_name,
                                           opponent_board.hero_card_id,
                                           opponent_board.player_id)
        headline = self.t("odds.vs", name=opponent_name)
        subtitle = self.t("odds.opening")

        odds = OddsView(
            headline=headline,
            subtitle=subtitle,
            win=result.win_pct,
            tie=result.tie_pct,
            loss=result.loss_pct,
            avg_damage_dealt=result.avg_damage_dealt,
            avg_damage_taken=result.avg_damage_taken,
            max_damage_taken=result.max_damage_taken,
            lethal_risk=result.lethal_risk(effective_health),
            coverage=coverage,
            unknown_cards=unknown,
            iterations=result.iterations,
            elapsed=result.elapsed,
            margin=result.margin_of_error,
        )
        self._last_result = (result, odds)
        self._remember_prediction(record, odds)
        return odds

    def _remember_prediction(self, record, odds: OddsView) -> None:
        """Pin the prediction to the fight it describes.

        Kept as a plain dict on the record: the journal writes it out much later,
        once the fight has resolved, and by then the OddsView is long gone. The
        record is the one the boards came from — see :meth:`_matchup` for why
        re-reading the live one here was wrong.
        """
        if record is None:
            return
        record.prediction = {
            "win": round(odds.win, 1),
            "tie": round(odds.tie, 1),
            "loss": round(odds.loss, 1),
            "avg_damage_dealt": round(odds.avg_damage_dealt, 1),
            "avg_damage_taken": round(odds.avg_damage_taken, 1),
            "max_damage_taken": odds.max_damage_taken,
            "lethal_risk": round(odds.lethal_risk, 1),
            "coverage": round(odds.coverage, 3),
            "unknown_cards": list(odds.unknown_cards),
            "iterations": odds.iterations,
            "margin": round(odds.margin, 1),
        }

    def _on_combat_scored(self, record) -> None:
        if not self.settings.debug_log or self.replaying:
            return
        path = self.journal.record(record)
        if path is None and self.journal.error:
            self.warnings.append(self.t("warn.journal", error=self.journal.error))

    def set_debug_log(self, enabled: bool) -> Path:
        """Turn the journal on or off; returns the directory it writes to."""
        self.settings.debug_log = bool(enabled)
        try:
            self.settings.save()
        except OSError:
            pass
        return self.journal.directory

    # ------------------------------------------------------------ view model

    def _hero_name(self, card_id: str) -> str:
        if not card_id:
            return ""
        return self.db.name(card_id, fallback="") if self.db.loaded else card_id

    def _player_label(self, name: str, hero_card_id: str, player_id: int) -> str:
        """Opponent nicknames are learned from combat tags, and a few of those
        lines carry a localised *card* name (Bob, Kel'Thuzad) instead of a real
        account. Fall back to the hero whenever the name is actually a card."""
        if name and name.lower() not in self._card_names:
            return name
        hero = self._hero_name(hero_card_id)
        return hero or (self.t("row.player", id=player_id) if player_id else "?")

    def _minion_views(self, board: BoardSnapshot) -> list[MinionView]:
        out = []
        for m in board.minions:
            keywords = "".join(c for c, on in (
                ("T", m.taunt), ("D", m.divine_shield), ("P", m.poisonous or m.venomous),
                ("R", m.reborn), ("W", m.windfury)) if on)
            out.append(MinionView(
                card_id=m.card_id,
                name=self.db.name(m.card_id, m.name) if self.db.loaded else (m.name or m.card_id),
                attack=m.attack, health=m.health, tier=m.tier,
                golden=m.golden, keywords=keywords,
                image=self.art.path_for(m.card_id) or ""))
        return out

    def _my_life(self, record=None) -> tuple[int, int]:
        """(health, armor) as they stand on screen right now.

        The log runs 10-30 seconds ahead of the animation: the moment a fight is
        dumped, its hero damage is already applied to the live player state,
        while the player is still watching the fight with the old numbers. Using
        the live value there both contradicts the screen and, worse, measures
        lethal risk against health the fight has not cost yet — a 15-damage
        prediction read as certain death against 13 hp when the fight actually
        started from 28. The combat record keeps the pre-fight numbers.

        ``record`` names the fight to answer for. The view model wants the one
        on screen and passes nothing; the simulator passes the fight it is
        scoring, so the lethal-risk figure is measured against the health that
        fight started from rather than whatever is current when it finishes.
        """
        me = self.state.players.get(self.state.my_player_id)
        if me is None:
            return 0, 0
        record = record or self.state.current_combat
        if record is None or self.state.display_phase() != "combat":
            return max(0, me.health - me.damage), me.armor
        damage, armor = record.my_health_before
        return max(0, me.health - damage), armor

    def _build_model(self, odds: Optional[OddsView], pending: bool = False) -> ViewModel:
        state = self.state
        me = state.players.get(state.my_player_id)
        health, armor = self._my_life()

        # No match on screen means no live readout. The log keeps the finished
        # game around, so without this the turn counter, the odds card and the
        # hover pins all stay up over the Battlegrounds menu, describing a lobby
        # that ended minutes ago.
        live = state.game_active and state.is_battlegrounds and state.in_gameplay

        model = ViewModel(
            connected=live,
            turn=state.display_turn if live else 0,
            phase=state.display_phase() if live else "idle",
            my_health=health,
            my_armor=armor,
            my_tier=me.tech_level if me else 1,
            odds=odds if live else None,
            odds_pending=pending and live,
            warnings=list(self.warnings),
            language=self.language,
        )

        if not state.game_active or not state.in_gameplay:
            model.status = self.t("status.waiting_match")
        elif not state.is_battlegrounds:
            model.status = self.t("status.other_match")
        elif state.game_over:
            model.status = self.t("status.game_over")
        elif model.in_combat:
            model.status = self.t("status.combat")
        else:
            model.status = self.t("status.tavern")

        if self.settings.show_opponents:
            model.opponents = self._opponent_views()
            # Warm the art cache now; hovering should not wait on the network.
            self.art.prefetch(m.card_id for board in state.last_boards.values()
                              for m in board.minions)
        if self.settings.show_leaderboard:
            model.leaderboard = self._leaderboard_views()
        if self.settings.show_pool and self.db.loaded:
            model.pool = [
                PoolEntry(card_id=p.card_id, name=p.name, tier=p.tier, seen=p.seen,
                          remaining=p.remaining, total=p.total)
                for p in pool_module.build(state, self.db, limit=60, min_tier=1)
            ]
        if self.settings.show_stats:
            model.stats = self._stat_lines(me)
        model.hero_choices = self._hero_choice_views()
        model.history = self._history_lines()
        return model

    def _opponent_views(self, limit: int = 8) -> list[OpponentView]:
        out = []
        boards = self.state.opponent_boards()
        # The next opponent first — that is the board that matters right now.
        boards.sort(key=lambda b: (b.player_id != self.state.next_opponent_id, -b.turn))
        for board in boards[:limit]:
            info = self.state.players.get(board.player_id)
            out.append(OpponentView(
                player_id=board.player_id,
                name=self._player_label(board.player_name or (info.name if info else ""),
                                        board.hero_card_id, board.player_id),
                hero=self._hero_name(board.hero_card_id),
                tier=info.tech_level if info else board.tech_level,
                health=max(0, (info.health - info.damage)) if info else board.hero_health,
                armor=info.armor if info else 0,
                place=info.place if info else 0,
                dead=info.dead if info else False,
                turn_seen=board.turn,
                is_next=board.player_id == self.state.next_opponent_id,
                has_board=bool(board.minions),
                minions=self._minion_views(board),
            ))
        return out

    def _leaderboard_views(self) -> list[OpponentView]:
        out = []
        for info in self.state.leaderboard():
            if info.player_id == self.state.ghost_player_id:
                continue
            name = self._player_label(info.name, info.hero_card_id, info.player_id)
            if info.player_id == self.state.my_player_id:
                name = self.t("row.me")
            board = self.state.last_boards.get(info.player_id)
            out.append(OpponentView(
                player_id=info.player_id,
                name=name,
                hero=self._hero_name(info.hero_card_id),
                tier=info.tech_level,
                health=max(0, info.health - info.damage),
                armor=info.armor,
                place=info.place,
                dead=info.dead,
                is_next=info.player_id == self.state.next_opponent_id,
                is_me=info.player_id == self.state.my_player_id,
                has_board=board is not None and bool(board.minions),
            ))
        return out

    def _stat_lines(self, me) -> list[StatLine]:
        lines: list[StatLine] = []
        personal = self.personal
        if personal.games:
            lines.append(StatLine(self.t("stat.avg_place"),
                                  f"{personal.average_placement:.2f}",
                                  self.t.plural(personal.games, "games")))
        total_combats = personal.combat_wins + personal.combat_ties + personal.combat_losses
        if total_combats:
            lines.append(StatLine(self.t("stat.combat_wins"),
                                  f"{personal.combat_win_rate:.0f}%",
                                  self.t.plural(total_combats, "fights")))
        if me is not None:
            record = personal.record_for(me.hero_card_id)
            if record is not None and record.games:
                lines.append(StatLine(self.t("stat.with_hero",
                                             hero=record.hero_name or "—"),
                                      f"{record.average_placement:.2f}",
                                      self.t.plural(record.games, "games")))
            entry = self.external.hero(me.hero_card_id) if self.external.available else None
            if entry:
                lines.append(StatLine(self.t("stat.global_place"),
                                      f"{entry.get('averagePlacement', 0):.2f}",
                                      self.t.plural(int(entry.get("games", 0)), "games")))
        return lines

    def _hero_choice_views(self) -> list[HeroChoiceView]:
        """Heroes on offer, each with whatever win data we can honestly show."""
        out = []
        for card_id in self.state.hero_choices():
            record = self.personal.record_for(card_id)
            entry = self.external.hero(card_id) if self.external.available else None
            out.append(HeroChoiceView(
                card_id=card_id,
                name=self._hero_name(card_id) or card_id,
                personal_avg=record.average_placement if record else 0.0,
                personal_games=record.games if record else 0,
                global_avg=float(entry.get("averagePlacement", 0)) if entry else 0.0,
                global_games=int(entry.get("games", 0)) if entry else 0,
                image=self.art.path_for(card_id) or "",
            ))
        return out

    # ------------------------------------------------------------------ hover

    def resolve_hover(self, hit, panel_key: Optional[str]) -> Optional[PopupView]:
        """What to show next to the cursor, given what it is pointing at.

        ``panel_key`` comes from our own panels; ``hit`` from the geometric
        guess about the Hearthstone window underneath.
        """
        model = getattr(self, "last_model", None)
        if model is None:
            return None

        if panel_key and panel_key.startswith("hero:"):
            return self._opponent_popup(int(panel_key.split(":", 1)[1]), model)
        if panel_key:
            return None

        if hit is None:
            return None
        if hit.kind == "hero":
            board = model.leaderboard
            if hit.index >= len(board):
                return None
            row = board[hit.index]
            if row.is_me:
                return None
            return self._opponent_popup(row.player_id, model)
        if hit.kind == "tavern":
            return self._tavern_popup(hit.index, model)
        return None

    def _opponent_popup(self, player_id: int, model) -> Optional[PopupView]:
        info = self.state.players.get(player_id)
        board = self.state.last_boards.get(player_id)
        name = self._player_label(info.name if info else "",
                                  info.hero_card_id if info else "", player_id)
        if board is None or not board.minions:
            # Nothing to show, and no need to say so: the pin on that portrait is
            # already grey, which is exactly what "not seen yet" looks like.
            return None
        stale = max(0, self.state.turn - board.turn)
        when = (self.t("popup.ago", turns=self.t.plural(stale, "turns")) if stale
                else self.t("popup.just_now"))
        subtitle = self.t("popup.board_turn", turn=board.turn) + " · " + when
        if info is not None:
            subtitle += " · " + self.t("row.tier", tier=info.tech_level)
        return PopupView(kind="opponent", title=name, subtitle=subtitle,
                         minions=self._minion_views(board),
                         accent="bad" if player_id == self.state.next_opponent_id
                                else "normal")

    def _tavern_popup(self, index: int, model) -> Optional[PopupView]:
        tavern = self.state.current_tavern()
        if index >= len(tavern):
            return None
        minion = tavern[index]
        name = (self.db.name(minion.card_id, minion.name) if self.db.loaded
                else (minion.name or minion.card_id))
        entry = next((p for p in model.pool if p.card_id == minion.base_card_id), None)
        total = pool_module.POOL_SIZE_BY_TIER.get(minion.tier, 0)
        seen = entry.seen if entry is not None else 0
        remaining = entry.remaining if entry is not None else total
        lines = []
        if total:
            lines.append(self.t("popup.pool_left", left=remaining, total=total))
            lines.append(self.t("popup.pool_seen", seen=seen))
        text = self.db.text(minion.card_id) if self.db.loaded else ""
        if text:
            lines.append(_wrap(text, 40))
        return PopupView(kind="minion", title=name,
                         subtitle=self.t("popup.minion", tier=minion.tier,
                                         attack=minion.attack, health=minion.health),
                         lines=lines,
                         accent="bad" if remaining <= 2 else "normal")

    def _history_lines(self, limit: int = 5) -> list[str]:
        out = []
        for record in self.state.combat_history[-limit:]:
            if not record.actual_result:
                continue
            mark = {"win": "W", "loss": "L", "tie": "T"}[record.actual_result]
            info = self.state.players.get(record.opponent_player_id)
            name = self._player_label(info.name if info else "",
                                      info.hero_card_id if info else "",
                                      record.opponent_player_id)
            damage = f" {record.actual_damage}" if record.actual_damage else ""
            out.append(self.t("history.line", mark=mark, turn=record.turn,
                                name=name, damage=damage))
        return out


def _wrap(text: str, width: int) -> str:
    """One-line truncation — popups stay compact on purpose."""
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"
