# Accuracy and internals

How well the simulator predicts a fight, how that is measured, what is still
missing from it, and the tools used to find out.

[Русская версия](ACCURACY.ru.md) · [back to the README](../README.md)

---

## Simulator accuracy

The simulator models **exactly** everything the log contains: attack, health,
Divine Shield, Taunt, Poisonous/Venomous, Reborn, Windfury, plus attack order,
target selection and damage to the hero.

The key trick: the snapshot of both boards is taken **right before the first
attack**, not at the start of the combat phase. By then the game has already
applied every start-of-combat effect — hero powers, trinkets, Rally, auras — and
they land in the stats for free, without a single line of modelling.

The trick has exactly one hole: a hero power that reacts to an event which has
**not happened yet** at snapshot time. Those are read from the database by the
hero's `heroPowerDbfId`, and there are three of them:

| Power | What it does in the fight |
|---|---|
| Greybough | "+1/+2 and Taunt to minions summoned in combat" — two identical 1/1 boards diverge into a loss: his skeletons come up 2/3 with Taunt, yours stay 1/1 |
| Rokara | "when a friendly minion kills an enemy, give it +1 Attack permanently" — the counter grows mid-fight |
| Drek'Thar, Vanndar | "when a space opens in combat, summon a copy of the strongest/toughest minion" — only armed if the board entered combat **full**: with 6 minions the game already summoned the copy before the snapshot, and it is in it |

Every other power either works in the tavern or fires at the start of combat —
both are already baked into the snapshot's stats.

**Trinkets.** Of 390 trinkets, 283 work in the tavern only; of the rest, two
classes act in combat in a way the snapshot cannot show:

| Class | What is modelled |
|---|---|
| "when a space opens, summon…" | Automaton Portrait (the real 3/4 Ancestral Automaton), Boom Controller (an exact copy of the first Mech that fell — buffs included, not a clean one). Armed like Drek'Thar's: unconditional ones only if the board entered combat full, death-driven ones always |
| `Avenge (N)` | the counter lives **on the board**, not on a minion: a trinket has no body, it cannot be killed or silenced. Bird Feeder, Beetle Band, Staff of the Scourge, Gilnean Thorned Rose |

The ids are read from the log — yours by controller, the opponent's by id from
the combat setup block (the opponent gets a fresh proxy controller every fight,
so filtering by it is impossible). Placeholder "Lesser/Greater Trinket" cards are
dropped. Trinket coverage on boards went from **0% to 56%**: the `trinkets` field
existed from the start and reached the simulator, but was never filled.

**Tribes on tokens.** "Summon a 2/2 Beetle" used to parse into a nameless body
with no tribe, so every tabular buff ("give your Beasts +4/+4") missed the very
bodies the card had just created. The real card is now looked up by the name in
the text — id, tribe, tier and keywords — while the printed stats stay
authoritative.

Mechanics the log does not contain are extracted **automatically from card text**
in the English HearthstoneJSON database into a machine form and played by the
engine:

| Mechanic | What is modelled |
|---|---|
| Deathrattle | token summons (with the real stats of the named card), random summons from the pool ("2 random Deathrattle minions", "a random Beast from tier 2–4", "4 random Pirates"), damage (random / all / weakest / adjacent), buffs, keyword grants, stat transfers, killing or buffing the killer, returning fallen Mechs from the graveyard, summoning from hand (by attack, by health, random — with a buff on the way out), "make the one on the right golden", triggering another minion's deathrattle |
| **Rally** | the struck target: damage to it and its neighbours, "damage equal to its own attack", "set its stats to 3/3", "remove its Reborn and Taunt", "steal its attack"; plus positional buffs, Blood Gems on itself and on the rest of the board, doubling its own stats, "gain attack equal to your tier", self-destruction after the hit, triggering another deathrattle (Monstrous Macaw), "once/twice per combat" limits |
| Avenge | trigger threshold, buffs, damage, Blood Gems |
| Frenzy | fires once, on the first damage survived |
| "When this takes damage" | buffs, summons, "twice per combat" limits |
| "After this kills a minion" | stat growth, "gain its max stats", separately "after it attacks and kills" |
| "When a friendly minion dies" | Avenge counters, "gain its attack/stats", **"gain its deathrattle"** (Fish of N'Zoth accumulates other deathrattles) |
| "Whenever you summon a minion" | both "during combat" and "in combat" — buff the summoned minion, buff itself, keywords |
| Cleave, shield loss | as before |
| **Auras over other triggers** | Titus Rivendare: "your deathrattles trigger twice" — the multiplier is read **at the moment the deathrattle fires**, so a Titus who fell in the same wave of deaths doubles nothing; golden gives ×3. A deathrattle triggered by another card (Monstrous Macaw, Deathstrider) is multiplied too, as in the game |
| **"After a friendly Rally minion attacks"** | Deathstrider: "trigger your leftmost deathrattle" (golden: twice). A separate hook from the usual "when a friendly minion attacks": it looks for the Rally keyword on the attacker and fires **after** the exchange, not before. A Windfury minion runs it twice per turn |
| **"Your Beetles get +5/+5 this game"** | not a buff on standing bodies but an order applying to every token summoned from then on: it accumulates on the board, survives the death of whoever granted it, and reaches every new Beetle at summon time |

### The board that grows itself

The Beetle board — the fight in the journal that was promised 0% and won —
stands on three mechanics, each harmless on its own: **Deathstrider** triggers
the leftmost deathrattle after every Rally attack, **Turquoise Skitterer** grants
Beetles +5/+5 "this game" through that deathrattle, and **Titus Rivendare**
doubles both. A golden Gryphon with Windfury runs the chain twice a turn. None of
the three was parsed: the engine saw vanilla bodies and confidently called the
fight lost.

Order inside a deathrattle matters exactly as much as in the card text: "Your
Beetles get +5/+5 this game. **Then** summon a 2/2 Beetle" — the order first,
the body second, otherwise the first Beetle comes out without the bonus the card
just granted.

**Measured.** 212 fights from three recorded logs, the same set, run twice — with
the old effect table and the new one. Outcomes guessed 80.2% → 80.7%, Brier
0.1233 → 0.1208, "confidently wrong" (under 2% probability on what happened)
19 → 16. Three fights moved by more than 5 points, all three toward what actually
happened: `84→98%`, `86→92%`, `34→82%`, and all three were wins. No fight moved
the wrong way. The gain is modest precisely because the package is narrow: it
came up three times in two hundred.

**What is still missing.** The bonus accumulated over previous turns is not
visible in the snapshot: the fight starts at "Beetles +0/+0" even when the game
has +150/+150 there by then. Predictions are low as a result, so such cards are
**deliberately marked as not fully parsed** (`game_carryover`) — the overlay
shows "accuracy ~71%" instead of certainty about a loss.

Three tricks that raised coverage by dozens of cards at once:

* **A shared trigger header.** "Battlecry, Deathrattle, and Rally: give the
  others +2/+2" — one text for three triggers. The splitter looked for the
  `Deathrattle:` marker, did not find it and filed the card as a vanilla body.
  Such a header is now expanded into a separate sentence per trigger.
* **Spells by name.** "Rally: Cast Queen's Command" — the spell itself is almost
  always a plain buff, so its text is substituted into the clause before parsing.
* **Plural tribes.** "4 random Pirates" did not match the `PIRATE` tribe, because
  the table holds the singular — and summoned nothing.

Whatever cannot be derived from text lives in `MANUAL_EFFECTS` (currently empty —
not one card had to be written by hand).

### Measured on real fights from your own logs

`tools/accuracy.py` compares each prediction against the **actual** outcome of
that fight, taken from the same log.

The outcome comes from the log itself: the game prints `META_DATA - Meta=DAMAGE`
naming whose hero took damage — that *is* the result, no guessing. It used to be
derived from the health difference between fights, and the labels lied: a fight
of "my 4/4 against a 1/1" was recorded as a loss. Of 279 fights, 245 are now
labelled directly by the log; the remaining 34 (mostly ties, where nobody takes
damage) by the old heuristic.

Measured on 206 fights from fresh logs (after that fix / before it):

| Metric | Now | Before |
|---|---|---|
| Most likely outcome guessed | 79.1% | 79.7% |
| Brier score of the win probability | 0.132 | 0.126 |
| Mean damage error | 2.0 | 1.9 |
| Card coverage | **97%** | 95% |
| Fights parsed **completely** | **168 of 206** | 138 of 202 |

An honest result: the summary numbers did not move — they wander ±1.5 pp from run
to run, and the new mechanics were simply rare in these fights. What moved is
something else: 30 more fights are now parsed **in full**, so the overlay stopped
attaching "accuracy ~85%" to them.

An earlier measurement over 286 fights:

| Metric | Without the damage cap | With it |
|---|---|---|
| Most likely outcome guessed | 77.5% | 77.5% (±0.8) |
| Brier score of the win probability | 0.162 | 0.162 (0.25 = a coin) |
| Mean damage error | 4.3 | **2.6** |
| Predictions above the game's cap | 67 of 284 | **0** |
| Card coverage | 98% | 98% |

The share of guessed outcomes wanders ±1.5 pp between runs: in plenty of fights
the odds are near 50/50, and "the most likely outcome" flips there on the random
draw. The Brier score is stable — that is the number to compare.

**The damage cap.** Battlegrounds limits the damage of a lost fight: 2 early in
the match, then 5, 10 and 15 — the game writes it in the `BACON_COMBAT_DAMAGE_CAP`
tag. The simulator could account for it, but the application never passed the
value, so the overlay promised "damage ~32" where the game could not take more
than 15. The cap is now read from the log and reaches both the damage forecast
and the lethal risk.

**What remains.** Over 286 fights, 32 times (11%) the engine was 90%+ confident
and wrong. Those are real simulator misses rather than bad labels, and going
through them one by one continues.

The overlay shows `accuracy ~85% · not modelled: <cards>` when the boards hold
cards with an unparsed combat trigger — treat the win percentage with more
suspicion then. Triggers that fire in the tavern or in hand ("gain a Blood Gem",
"give a minion in hand +7/+7") do not lower coverage: they do not affect this
fight. Neither does start-of-combat — that is already baked into the stats.

Re-measure on your own logs at any time:

```bash
./run.sh --tool accuracy
```

### Performance

Measured on this machine (Apple Silicon, 803 MB of logs):

| Stage | Result |
|---|---|
| Log parsing | 185k lines/s — ~185× headroom against a live game |
| A 7×7 fight, 2000 runs | 0.53 s over 8 processes (3,772 runs/s) |
| Overlay at rest | 2–3% of one core, 120 MB |
| First parse of the history | ~30% of one core, then 0.9 s from cache |

Python is not the bottleneck here. The prediction is computed once per fight and
takes half a second against 8–40 seconds of animation, and accuracy is not
limited by the number of runs: 2000 iterations give ±2.2 pp of statistical
spread, while the systematic error from unmodelled cards is around ±10 pp.
Rewriting the engine in Rust would remove the ±2.2 and leave the ±10 untouched.

Where a fast language would genuinely help is searching minion placements
(7! = 5040 orders × 1000 runs ≈ 5M fights). That is ~12 minutes in Python and
seconds in Rust. If that suggestion is ever wanted, the thing to move into a
native module is the combat engine alone.

### Comparing trajectories: what to fix next

Outcome measurement (`tools/accuracy.py`) says *how often* the engine is wrong,
never *why*: a 7×7 fight is forty swings, and one wrong rule anywhere flips the
result. But `Power.log` names **both sides of every swing** — the
`PROPOSED_ATTACKER` / `PROPOSED_DEFENDER` tags inside each `ATTACK` block (the
block header's `Target=` is always 0, there is nothing there). That is a ready
list of the game's own moves.

`tools/divergence.py` pins the simulator to that list: at every step it asks the
engine who it would attack with, compares against reality and **forces** the real
choice, so the replay never leaves the true trajectory. Target randomness stops
interfering and only rule mismatches remain, of two kinds:

| | what it means |
|---|---|
| `order` | the engine picked a different attacker — attack pointer, summon position, Windfury accounting |
| `state` | the engine thinks the real attacker or its target is already dead — an effect was not played |

Bodies summoned during a fight have no id in the simulator, so they are matched
to log ids by card. If they cannot be matched, the replay **stops** rather than
reporting a divergence: leaving the tracked zone is not an engine bug, and
counting it as one would drown the real findings. Only the **first** divergence
in a fight counts — everything after it follows from an already-diverged board.

```bash
./run.sh --tool divergence -- --min-swings 2
```

Recording the course of a fight (`CombatRecord.trace`) is enabled by the
`trace_combat` flag and off by default — a live overlay has no use for it.

The tool immediately reordered the priorities. The hypothesis "attack order is to
blame" was **not confirmed**; the blame is on bodies the engine does not create.
Over 194 fights:

| | before | after |
|---|---|---|
| watched to the last swing | 32.1% | **37.1%** |
| left the tracked zone | 59.8% | **51.5%** |
| median share of swings before stopping | 42% | **46%** |
| Brier | 0.140 | **0.137** |

It also prints a **ranked list of cards** the game summoned and the simulator did
not — a work queue instead of guesswork.

### The combat journal (debugging)

The **BG** menu bar item has a **"Debug: combat journal"** toggle and an **"Open
journal folder"** item next to it. Off by default — it is diagnostics, not
something a normal session needs.

When on, every finished fight appends one JSON line to
`~/Library/Application Support/hsbg-overlay/debug/combats-YYYY-MM-DD.jsonl`: the
prediction, the actual outcome, **both boards in full** (stats, keywords,
positions, trinkets, hand) and a `surprise` field — how much probability the
prediction gave to anything other than what happened. The boards are written in
full deliberately: `Power.log` rotates and runs to tens of millions of lines,
while this one line reproduces the fight a week later.

Read the journal:

```bash
./run.sh --tool debugreview -- --boards
```

It prints a calibration table ("of the fights promised 0–2%, how many were
actually won") and sorts the misses by confidence. The most valuable case is
**0% and a win**: that is not variance — 2000 runs found no path to victory, so
the board in the simulator was not the board in the game.

The very first run over recorded logs found exactly such a fight — a giant board
lost to seven small minions — and named the culprits: **Titus Rivendare** and
**Deathstrider**. The engine played neither **and did not mark them as
unparsed**, so the overlay showed "100% coverage" next to a 100% wrong
prediction. First came the marking, then the modelling — see "The board that
grows itself" above.

**Two traps in the journal itself**, which made it lie about what happened.

*The prediction was attached to the wrong fight.* The simulation runs on its own
thread for a second or more, and the log reader does not wait. The finished
answer used to be stitched onto `state.current_combat` **at the moment the
computation ended** — that is, onto whichever fight had become current by then.
In normal play that is a rare race, but the overlay starts by reading the log
from the last `CREATE_GAME`, and a whole played-out match flies through the
parser in a couple of seconds, where it is the rule rather than the exception. In
the journal it looked like "promised 0%, won": the numbers were honest, just from
a different turn. The fight now travels together with its boards from
`_matchup()` to the write.

*One match was written to the journal on every restart.* The same startup
re-read replays all past fights and honestly "counts" each one — with an empty
prediction, because there was nothing to predict. Duplicates piled up in the
file and the calibration table counted them as separate observations. The log
reader now reports whether it has caught up with the writer
(`PowerLogTailer.caught_up`), and fights from that tail do not reach the journal.

## What is not modelled

74 unparsed combat triggers remain across 71 cards (counting golden versions
separately) — 42 unique cards, 14 of them marked only for the invisible
"accumulated this game" tail. What is left:

* **trinkets** beyond the two parsed classes: those that grant minions a
  deathrattle ("give your Quilboar 'Deathrattle: gain 2 Blood Gems'") and those
  that depend on "the first minion summoned this combat" (Twin Sky Lanterns);
* **Blood Gems as a counter**: "summon a Golem with stats equal to this minion's
  Blood Gems" (Corrupted Bristler, Prison Juggernaut, Warped Bristler) — how many
  gems are on a minion is not visible in the snapshot;
* **immediate attacks**: "your leftmost minion immediately attacks whatever
  killed this" (Seasoned Technician), Onyxia's whelps — the engine can summon the
  body but not give it an out-of-order swing;
* **other minions' battlecries**: "trigger a neighbour's battlecry" (Rylak,
  Gardener) — battlecries are not played in combat at all;
* **stat thresholds**: "once its attack reaches 6, gain Divine Shield" (Crimson
  Survivor) — nobody watches stats mid-fight;
* **transformations**: "Avenge (5): become a copy of the minion on the left"
  (Karmic Chameleon), magnetising from a deathrattle (Apexis Sentinel);
* **accumulated "this game"**: how many times your Beetles were given +5/+5 over
  the past ten turns is written nowhere in the snapshot; only what accumulates
  inside the fight itself is modelled;
* cards that depend on the **opponent's hand** — the hand is simply not in the
  log, and that is fundamental: your own hand is accounted for, theirs never is;
* Duos (paired Battlegrounds).

**Reborn and Divine Shield.** There was a hypothesis that Reborn brings a card
back with its *printed* keywords, i.e. restores a popped Divine Shield.
Implemented, measured over 207 fights — and accuracy fell from 79.7% to 74.4%,
Brier from 0.128 to 0.181. The game does not restore the shield; the body comes
back exactly as it fell, minus its life and minus Reborn itself. The hypothesis
was reverted and the finding written into a comment in `engine.summon_reborn` so
nobody reopens it.

A fresh list at any time:

```bash
./.venv/bin/python -c "from hsbg.carddb import get_db; db = get_db(); spec = db.derive_effects(); print('\n'.join(sorted({db.name(i) + ' ' + str(s['unmodelled']) for i, s in spec.items() if s.get('unmodelled')})))"
```
