"""Finding, repairing and following Hearthstone's per-launch logs.

Hearthstone creates a fresh ``Hearthstone_<timestamp>/`` folder every launch and
appends to ``Power.log`` inside it. The tailer therefore has to follow one file
*and* notice when the game restarts into a new folder. The same tailer follows
``LoadingScreen.log``, which is the only place the game says which screen the
player is actually looking at.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Iterator, Optional

from .config import LOG_CONFIG_PATH, LOG_ROOTS, REQUIRED_LOG_SECTIONS


def newest_log_dir() -> Optional[Path]:
    """Most recently written per-launch log folder across all known roots."""
    candidates: list[tuple[float, Path]] = []
    for root in LOG_ROOTS:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if not child.is_dir() or not child.name.startswith("Hearthstone_"):
                continue
            power = child / "Power.log"
            if power.exists():
                candidates.append((power.stat().st_mtime, child))
    if not candidates:
        return None
    return max(candidates)[1]


def parse_log_config(text: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, {})
        elif current and "=" in line:
            key, _, value = line.partition("=")
            sections[current][key.strip()] = value.strip()
    return sections


def render_log_config(sections: dict[str, dict[str, str]]) -> str:
    out = []
    for name, entries in sections.items():
        out.append(f"[{name}]")
        out.extend(f"{k}={v}" for k, v in entries.items())
    return "\n".join(out) + "\n"


def ensure_log_config() -> tuple[bool, str]:
    """Make sure Hearthstone will actually write the lines we need.

    Returns ``(changed, message key)`` — a key for :mod:`hsbg.i18n` rather than
    a sentence, since the caller shows it in the game's language. A change only
    takes effect after the game is restarted, so the caller should say so.
    """
    try:
        existing = LOG_CONFIG_PATH.read_text("utf-8", errors="replace")
    except OSError:
        existing = ""
    sections = parse_log_config(existing)

    changed = False
    for name, required in REQUIRED_LOG_SECTIONS.items():
        section = sections.setdefault(name, {})
        for key, value in required.items():
            if section.get(key, "").lower() != value.lower():
                section[key] = value
                changed = True

    if changed:
        LOG_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_CONFIG_PATH.write_text(render_log_config(sections), "utf-8")
        return True, "logcfg.updated"
    return False, "logcfg.ok"


CREATE_GAME_MARKER = b"GameState.DebugPrintPower() - CREATE_GAME"

# LoadingScreen.log announces every screen change twice: once when the old scene
# starts unloading (``nextMode``) and once when the new one is up (``currMode``).
# Both are taken — leaving a match is announced a good three seconds before the
# menu finishes loading, and the overlay should be gone by then, not after.
RE_SCENE_UNLOAD = re.compile(r"LoadingScreen\.OnScenePreUnload\(\) - "
                             r"prevMode=\S+ nextMode=(\S+)")
RE_SCENE_LOADED = re.compile(r"LoadingScreen\.OnSceneLoaded\(\) - "
                             r"prevMode=\S+ currMode=(\S+)")

# The scene a match is played in. Everything else — BACON (the Battlegrounds
# menu), HUB, COLLECTION, LOGIN — means the player is not in a game.
SCENE_GAMEPLAY = "GAMEPLAY"


def parse_scene(line: str) -> Optional[str]:
    """Scene name from one LoadingScreen.log line, or None if it is not one."""
    for regex in (RE_SCENE_UNLOAD, RE_SCENE_LOADED):
        match = regex.search(line)
        if match:
            return match.group(1)
    return None


def last_game_offset(path: Path, marker: bytes = CREATE_GAME_MARKER,
                     chunk_size: int = 1 << 20) -> int:
    """Byte offset of the last ``CREATE_GAME`` in the file.

    Power.log grows to tens of megabytes per session and the parser resets its
    whole state on CREATE_GAME anyway, so replaying earlier matches is wasted
    work. Scanning backwards from the end finds the current match in one or two
    chunk reads instead of parsing the entire file.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    overlap = len(marker) + 8
    try:
        with path.open("rb") as fh:
            position = size
            while position > 0:
                start = max(0, position - chunk_size)
                fh.seek(start)
                blob = fh.read(position - start + overlap)
                found = blob.rfind(marker)
                if found >= 0:
                    absolute = start + found
                    # Rewind to the beginning of that line.
                    line_start = blob.rfind(b"\n", 0, found)
                    return start + line_start + 1 if line_start >= 0 else absolute
                position = start
    except OSError:
        return 0
    return 0


class PowerLogTailer:
    """Yields lines from one launch-folder log, following rotation into new ones.

    ``from_start=True`` replays the current file from ``start_marker``, which
    lets the app rebuild the state of a match that is already in progress. With
    ``start_marker=None`` the whole file is replayed — right for LoadingScreen.log,
    which is a few thousand lines and whose *last* scene line is the one we need.
    """

    def __init__(self, from_start: bool = True, poll: float = 0.1,
                 filename: str = "Power.log",
                 start_marker: Optional[bytes] = CREATE_GAME_MARKER):
        # from_start means "replay the current match", not "replay the file".
        self.from_start = from_start
        self.poll = poll
        self.filename = filename
        self.start_marker = start_marker
        self.path: Optional[Path] = None
        # False until the reader has drained everything already in the file and
        # is waiting on the writer. Callers use it to tell "this happened while
        # I was away" from "this is happening now" — a match replayed from the
        # top on startup fires exactly the same events as a live one.
        self.caught_up = False
        self._fh = None
        self._inode: Optional[int] = None
        self._dir_check = 0.0

    def _open(self, path: Path, seek_end: bool) -> bool:
        self.close()
        try:
            self._fh = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            # A launch folder need not carry every log: LoadingScreen.log only
            # appears once its section is enabled and the game restarted. Keep
            # looking rather than killing the reader thread.
            self._fh = None
            return False
        if seek_end:
            self._fh.seek(0, 2)
        elif self.from_start and self.start_marker:
            self._fh.seek(last_game_offset(path, self.start_marker))
        # A file just opened has a backlog again, however short.
        self.caught_up = seek_end
        self.path = path
        try:
            self._inode = path.stat().st_ino
        except OSError:
            self._inode = None
        return True

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def _maybe_rotate(self) -> bool:
        """Switch to a newer launch folder if the game restarted."""
        now = time.monotonic()
        if now - self._dir_check < 2.0:
            return False
        self._dir_check = now
        newest = newest_log_dir()
        if newest is None:
            return False
        candidate = newest / self.filename
        if self.path is not None and candidate == self.path:
            # Same path, but the game may have truncated/recreated the file.
            try:
                if candidate.stat().st_ino == self._inode:
                    return False
            except OSError:
                return False
        # A brand new file is always read from its last CREATE_GAME: that is the
        # match currently being played.
        return self._open(candidate, seek_end=False)

    def lines(self, stop: Optional[callable] = None) -> Iterator[str]:
        newest = newest_log_dir()
        if newest is not None:
            self._open(newest / self.filename, seek_end=not self.from_start)

        while stop is None or not stop():
            if self._fh is None:
                if not self._maybe_rotate():
                    time.sleep(0.5)
                continue
            chunk = self._fh.readline()
            if chunk:
                yield chunk.rstrip("\n")
                continue
            # Caught up with the writer.
            self.caught_up = True
            self._maybe_rotate()
            time.sleep(self.poll)
        self.close()


def read_log_file(path: Path) -> Iterator[str]:
    """Offline replay of a finished log — used by the tests and tools/replay.py."""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            yield line.rstrip("\n")
