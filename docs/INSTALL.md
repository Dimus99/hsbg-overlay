# Installing HSBG Overlay

Step by step, from a clean Mac to an overlay on top of the game.
[Русская версия](INSTALL.ru.md) · [back to the README](../README.md)

## Before you start

| | |
|---|---|
| macOS | 12 (Monterey) or newer, Apple Silicon or Intel |
| Hearthstone | installed through Battle.net and launched at least once |
| Python | 3.13 — only if you run from source; the built `.app` carries its own |

The overlay only ever **reads** the log files Hearthstone writes itself. It does
not read the game's memory, does not inject anything and does not talk to
Blizzard's servers.

---

## 1. Install Python 3.13

Skip this if `python3 --version` already prints 3.11 or newer.

```bash
brew install python@3.13
```

No Homebrew? Install it from [brew.sh](https://brew.sh), or take the installer
from [python.org](https://www.python.org/downloads/macos/).

## 2. Get the code

```bash
git clone https://github.com/Dimus99/hsbg-overlay.git
cd hsbg-overlay
```

Without git: **Code → Download ZIP** on the GitHub page, unpack it, and `cd`
into the unpacked folder in Terminal.

## 3. Create the environment

```bash
./setup.sh
```

This makes a `.venv` next to the project and installs PyObjC into it — nothing
is installed system-wide. On Apple Silicon the script deliberately picks an
arm64 interpreter: the overlay draws through AppKit and has to match the
architecture of the machine.

Expected tail of the output:

```
Done. Next:
  ./run.sh --check    check the environment
  ./run.sh            start the overlay
```

## 4. Check the environment

```bash
./run.sh --check
```

```
— Environment check —
  log folder         : /Applications/Hearthstone/Logs/Hearthstone_2026_08_21_02_18_13
  log.config         : log.config already has everything we need.
  game language      : enUS (setting: auto)
  fullscreen         : no
  card database      : 5738 cards, 734 effects derived
  Hearthstone running: yes
  game window        : WindowRect(x=0.0, y=0.0, width=1440.0, height=900.0)
```

What each line has to say:

| Line | What you want to see |
|---|---|
| `log folder` | a path. "not found" means Hearthstone has never been launched, or it is installed somewhere other than `/Applications/Hearthstone` |
| `log.config` | either "already has everything" or "written" — in the second case **restart Hearthstone**, see step 5 |
| `game language` | the locale the game itself runs in; the overlay labels itself in it |
| `fullscreen` | `no`. A `YES` here means the overlay will stay behind the game — see step 6 |
| `card database` | thousands of cards. An error means no network on the first run; the database is downloaded once and then cached |
| `game window` | the size of the game's window. Empty is fine while Hearthstone is not running |

## 5. Restart Hearthstone once

On its first run the overlay writes the `[Power]` and `[LoadingScreen]` sections
into `~/Library/Preferences/Blizzard/Hearthstone/log.config`. Hearthstone reads
that file **at startup**, so the sections only take effect after a restart of
the game. Without them the log stays silent and the overlay has nothing to read.

Quit Hearthstone completely (⌘Q) and start it again.

## 6. Switch the game to windowed mode

macOS refuses to draw over an application in **exclusive fullscreen** — the game
moves to a Space of its own. In Hearthstone: **Settings → Graphics → Display
mode → Windowed** or **Borderless Window**.

Borderless Window looks exactly like fullscreen and is the usual choice.

Screen recording permission is **not** needed: window geometry comes from the
public window list.

## 7. Run it

```bash
./run.sh
```

A **BG** item appears in the menu bar. From there you can hide the overlay,
switch the "?" pins on the opponents' portraits on and off, turn on the debug
journal, or quit.

Start a Battlegrounds match — the status pill shows up in the corner of the game
window, and the odds card appears above the board once a fight starts.

---

## Optional: build a real .app

To launch it from Finder instead of a terminal:

```bash
./make_app.sh                 # builds into dist/
./make_app.sh /Applications   # builds and installs
```

`HSBG Overlay.app` (~23 MB) carries its own Python and PyObjC, so it depends on
neither this folder nor the `.venv`. Double-clicking it opens a window with a
**Start / Stop** button, an **environment check** button, and a live tail of the
log.

The bundle is ad-hoc signed, which is enough on the Mac that built it. A bundle
**downloaded** from GitHub Releases is quarantined by macOS and has to be
cleared once:

```bash
xattr -cr "/Applications/HSBG Overlay.app"
```

## Updating

```bash
git pull
./setup.sh     # only if requirements.txt changed
```

Settings and the card cache live outside the project, in
`~/Library/Application Support/hsbg-overlay/`, and survive any update.

## Uninstalling

```bash
rm -rf ~/Library/Application\ Support/hsbg-overlay
rm -f  ~/Library/Logs/HSBG-Overlay.log
rm -rf "/Applications/HSBG Overlay.app"     # if you installed the bundle
```

Then delete the project folder. The `log.config` sections the overlay added can
stay — they cost nothing and other trackers use the same ones.

---

## If something does not work

| Symptom | What it is |
|---|---|
| Overlay is nowhere on screen | the game is in exclusive fullscreen. `./run.sh --check` says so on the `fullscreen` line — switch to Windowed or Borderless Window |
| The pill says "waiting for a Battlegrounds match" during a match | the log sections are not active yet: restart Hearthstone once (step 5) |
| `log folder: not found` | Hearthstone has not been launched since installation, or it lives outside `/Applications/Hearthstone`. The overlay also looks in `~/Library/Logs/Hearthstone` |
| Card names are missing or the database errors out | no network on the first run. Connect once — the database is cached afterwards and `offline: true` in the settings keeps it from ever asking again |
| The "?" pins do not line up with the portraits | the hover zone is off; record it from two corners: `./run.sh --tool calibrate -- --set leaderboard` |
| `No virtualenv yet. Run ./setup.sh first.` | step 3 has not been run, or it failed — run it again and read the output |
| The `.app` will not open ("damaged") | a downloaded bundle under quarantine: `xattr -cr "/Applications/HSBG Overlay.app"` |
| Odds stay on screen after the fight | that is deliberate: they hide on your first click in the tavern, or after 60 seconds (`combat_hold_seconds`) |

Everything the overlay prints also goes to `~/Library/Logs/HSBG-Overlay.log` —
that is the first place to look when a start fails.
