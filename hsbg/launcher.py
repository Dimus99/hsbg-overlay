"""A window with one big button — the graphical way to start the overlay.

The overlay itself has no window: it lives in the menu bar and is normally
started from a terminal. This launcher is the alternative for everyone else.

The overlay runs as a *detached* child (``start_new_session``), so closing this
window leaves it running, and reopening the launcher finds it again by scanning
the process table. The launcher is a control panel, not a parent process you
have to keep alive.

Everything the overlay prints goes to ``~/Library/Logs/HSBG-Overlay.log``,
which the window tails — so a crash shows up here instead of vanishing.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import AppKit
import objc
from PyObjCTools import AppHelper

from .config import Settings
from .i18n import Strings, strings

PROJECT = Path(__file__).resolve().parent.parent
# Inside the built .app there is no project folder and no separate interpreter:
# the bundle re-launches *itself* with --overlay instead of "python -m hsbg".
FROZEN = getattr(sys, "frozen", False)
LOG_PATH = Path.home() / "Library" / "Logs" / "HSBG-Overlay.log"
APP_TITLE = "HSBG Overlay"

POLL_SECONDS = 1.0        # how often we re-check whether the overlay is alive
TAIL_BYTES = 64 * 1024    # enough log to read, cheap enough to re-read on a timer
TAIL_LINES = 400
KILL_GRACE = 4.0          # seconds between SIGTERM and SIGKILL
PENDING_GRACE = 6.0       # how long "Starting…" waits before believing ps

# Modes of ``python -m hsbg`` that are *not* the overlay: this window itself,
# and the diagnostics run it can start. Neither should read as running.
NOT_THE_OVERLAY = {"--launcher", "--check"}


# --------------------------------------------------------------- process side

def _is_overlay(parts: list[str]) -> bool:
    """Does this ``ps`` command line belong to a running overlay?

    Frozen, every mode runs the same executable, so the ``--overlay`` flag is
    the only thing that tells the overlay apart from this window (and from the
    simulation workers, which carry neither flag).
    """
    if FROZEN:
        return "--overlay" in parts
    if NOT_THE_OVERLAY.intersection(parts):
        return False
    return any(part == "-m" and parts[index + 1] == "hsbg"
               for index, part in enumerate(parts[:-1]))


def overlay_pids() -> list[int]:
    """PIDs of running overlays.

    Scanning ``ps`` rather than remembering the PID we spawned is what lets a
    second launcher — or one reopened after its window was closed — still see
    the overlay and offer to stop it.
    """
    try:
        listing = subprocess.run(["/bin/ps", "-Ao", "pid=,args="],
                                 capture_output=True, text=True,
                                 timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []

    me = os.getpid()
    pids: list[int] = []
    for line in listing.splitlines():
        pid_text, _, args = line.strip().partition(" ")
        if not pid_text.isdigit() or int(pid_text) == me:
            continue
        if _is_overlay(args.split()):
            pids.append(int(pid_text))
    return pids


def _log_banner(text: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", buffering=1) as handle:
        handle.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {text} ===\n")


def spawn(args: list[str], banner: str) -> None:
    """Run another copy of ourselves detached, with output going to the log."""
    _log_banner(banner)
    # Frozen, sys.executable *is* the app binary and takes the flags directly.
    command = [sys.executable, *args] if FROZEN \
        else [sys.executable, "-m", "hsbg", *args]
    workdir = str(Path.home()) if FROZEN else str(PROJECT)
    with open(LOG_PATH, "a", buffering=1) as handle:
        subprocess.Popen(command,
                         cwd=workdir,
                         stdin=subprocess.DEVNULL,
                         stdout=handle,
                         stderr=subprocess.STDOUT,
                         start_new_session=True)


def log_tail(limit: int = TAIL_LINES) -> str:
    try:
        with open(LOG_PATH, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - TAIL_BYTES))
            chunk = handle.read()
    except OSError:
        return ""
    lines = chunk.decode("utf-8", errors="replace").splitlines()[-limit:]
    return "\n".join(lines).strip()


# -------------------------------------------------------------------- window

WIDTH, HEIGHT = 520.0, 380.0
MARGIN = 20.0


def _rect(x: float, y: float, width: float, height: float):
    return ((x, y), (width, height))


def _label(frame, size: float, weight, color=None) -> AppKit.NSTextField:
    field = AppKit.NSTextField.alloc().initWithFrame_(frame)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(False)
    field.setFont_(AppKit.NSFont.systemFontOfSize_weight_(size, weight))
    if color is not None:
        field.setTextColor_(color)
    return field


def _button(frame, title: str, target, action: str) -> AppKit.NSButton:
    button = AppKit.NSButton.alloc().initWithFrame_(frame)
    button.setBezelStyle_(AppKit.NSBezelStyleRounded)
    button.setTitle_(title)
    button.setTarget_(target)
    button.setAction_(action)
    button.setAutoresizingMask_(AppKit.NSViewMinYMargin)
    return button


def _icon_path() -> Path | None:
    """The .icns to show in the Dock.

    The bundle passes its own Resources folder in ``HSBG_APP_ICON`` — that way
    the icon follows the .app if it is dragged to /Applications. Run from a
    terminal there is no bundle, so fall back to one built next to the project.
    """
    from_bundle = os.environ.get("HSBG_APP_ICON")
    candidates = [Path(from_bundle)] if from_bundle else []
    if FROZEN:
        # .../HSBG Overlay.app/Contents/MacOS/<exe> -> .../Contents/Resources
        candidates.append(Path(sys.executable).resolve().parent.parent
                          / "Resources" / "hsbg.icns")
    candidates.append(PROJECT / f"{APP_TITLE}.app" / "Contents" / "Resources" / "hsbg.icns")
    for path in candidates:
        if path.is_file():
            return path
    return None


def _adopt_identity(app) -> None:
    """Look like HSBG Overlay rather than like Python.

    Homebrew's ``python3`` re-executes itself out of Python.app, so the process
    inherits *that* bundle no matter what our .app says — the Dock shows the
    generic Python rocket and the menu bar says «Python». Overwriting the
    loaded info dictionary and the application icon is what pyobjc apps do
    instead of shipping a compiled launcher stub.
    """
    info = AppKit.NSBundle.mainBundle().infoDictionary()
    if info is not None:
        for key in ("CFBundleName", "CFBundleDisplayName"):
            try:
                info[key] = APP_TITLE
            except (TypeError, ValueError):
                pass  # an immutable dictionary: the Dock name stays «Python»

    icon = _icon_path()
    if icon is not None:
        image = AppKit.NSImage.alloc().initWithContentsOfFile_(str(icon))
        if image is not None:
            app.setApplicationIconImage_(image)


def _install_menu(app, t: Strings) -> None:
    """A minimal app menu — without one, ⌘Q does nothing."""
    bar = AppKit.NSMenu.alloc().init()
    app_item = AppKit.NSMenuItem.alloc().init()
    app_item.setTitle_(APP_TITLE)   # what the menu bar shows next to the apple
    bar.addItem_(app_item)

    menu = AppKit.NSMenu.alloc().init()
    menu.addItemWithTitle_action_keyEquivalent_(
        t("launcher.menu_hide", app=APP_TITLE), "hide:", "h")
    menu.addItem_(AppKit.NSMenuItem.separatorItem())
    menu.addItemWithTitle_action_keyEquivalent_(t("launcher.menu_close"),
                                                "performClose:", "w")
    menu.addItemWithTitle_action_keyEquivalent_(
        t("launcher.menu_quit", app=APP_TITLE), "terminate:", "q")
    app_item.setSubmenu_(menu)
    app.setMainMenu_(bar)


class Launcher:
    """Owns the window and the once-a-second liveness poll."""

    def __init__(self) -> None:
        # The launcher is a separate process from the overlay, so it works out
        # the game's language for itself rather than being told.
        self.t = strings(Settings.load().resolved_language())
        self.target = _Target.alloc().initWithLauncher_(self)
        self.window = None
        self.status = None
        self.toggle_button = None
        self.check_button = None
        self.text = None
        self._log_stamp: tuple = ()
        self._kill_deadline: float = 0.0
        # Optimistic status shown between a click and ps agreeing with it.
        self._pending = ""
        self._pending_running = False
        self._pending_until = 0.0

    # ---------------------------------------------------------------- build

    def build(self) -> None:
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
        app.setDelegate_(self.target)
        _adopt_identity(app)
        _install_menu(app, self.t)

        style = (AppKit.NSWindowStyleMaskTitled
                 | AppKit.NSWindowStyleMaskClosable
                 | AppKit.NSWindowStyleMaskMiniaturizable
                 | AppKit.NSWindowStyleMaskResizable)
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0.0, 0.0), (WIDTH, HEIGHT)), style, AppKit.NSBackingStoreBuffered, False)
        self.window.setTitle_("HSBG Overlay")
        self.window.setMinSize_((460.0, 320.0))
        self.window.center()

        content = self.window.contentView()

        title = _label(_rect(MARGIN, HEIGHT - MARGIN - 26, WIDTH - 2 * MARGIN, 26),
                       19, AppKit.NSFontWeightBold)
        title.setStringValue_(self.t("launcher.title"))
        title.setAutoresizingMask_(AppKit.NSViewMinYMargin | AppKit.NSViewWidthSizable)
        content.addSubview_(title)

        self.status = _label(_rect(MARGIN, HEIGHT - MARGIN - 56, WIDTH - 2 * MARGIN, 20),
                             13, AppKit.NSFontWeightRegular)
        self.status.setAutoresizingMask_(AppKit.NSViewMinYMargin | AppKit.NSViewWidthSizable)
        content.addSubview_(self.status)

        row = HEIGHT - MARGIN - 104
        self.toggle_button = _button(_rect(MARGIN, row, 190, 32),
                                     self.t("launcher.start"),
                                     self.target, "toggle:")
        self.toggle_button.setKeyEquivalent_("\r")
        content.addSubview_(self.toggle_button)

        self.check_button = _button(_rect(MARGIN + 200, row, 190, 32),
                                    self.t("launcher.check"), self.target, "check:")
        content.addSubview_(self.check_button)

        caption = _label(_rect(MARGIN, row - 30, WIDTH - 2 * MARGIN, 16), 11,
                         AppKit.NSFontWeightRegular,
                         AppKit.NSColor.secondaryLabelColor())
        caption.setStringValue_(self.t("launcher.log_caption", path=LOG_PATH))
        caption.setAutoresizingMask_(AppKit.NSViewMinYMargin | AppKit.NSViewWidthSizable)
        content.addSubview_(caption)

        log_height = row - 30 - 8 - MARGIN
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(
            _rect(MARGIN, MARGIN, WIDTH - 2 * MARGIN, log_height))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(AppKit.NSBezelBorder)
        scroll.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)

        self.text = AppKit.NSTextView.alloc().initWithFrame_(
            ((0.0, 0.0), scroll.contentSize()))
        self.text.setEditable_(False)
        self.text.setDrawsBackground_(False)
        self.text.setFont_(AppKit.NSFont.monospacedSystemFontOfSize_weight_(
            11, AppKit.NSFontWeightRegular))
        self.text.setVerticallyResizable_(True)
        self.text.setHorizontallyResizable_(False)
        self.text.setAutoresizingMask_(AppKit.NSViewWidthSizable)
        self.text.textContainer().setWidthTracksTextView_(True)
        scroll.setDocumentView_(self.text)
        content.addSubview_(scroll)

        self.window.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)

        AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            POLL_SECONDS, self.target, "tick:", None, True)
        self.tick()

    # --------------------------------------------------------------- actions

    def toggle(self) -> None:
        if overlay_pids():
            self.stop()
        else:
            self.start()

    def _expect(self, text: str, running: bool) -> None:
        self._pending = text
        self._pending_running = running
        self._pending_until = time.monotonic() + PENDING_GRACE

    def start(self) -> None:
        self._expect(self.t("launcher.starting"), True)
        self._kill_deadline = 0.0
        spawn(["--overlay"], self.t("launcher.banner_start"))
        self.refresh()

    def stop(self) -> None:
        self._expect(self.t("launcher.stopping"), False)
        for pid in overlay_pids():
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        self._kill_deadline = time.monotonic() + KILL_GRACE
        self.refresh()

    def check(self) -> None:
        """Run ``--check`` — its output lands in the log we are already tailing."""
        spawn(["--check"], self.t("launcher.banner_check"))

    # ---------------------------------------------------------------- polling

    def tick(self) -> None:
        if self._kill_deadline and time.monotonic() > self._kill_deadline:
            # SIGTERM went unheard — a hung event loop, most likely.
            for pid in overlay_pids():
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            self._kill_deadline = 0.0
        self.refresh()

    def refresh(self) -> None:
        running = bool(overlay_pids())
        if self._pending and (running == self._pending_running
                              or time.monotonic() > self._pending_until):
            # ps agrees with the button we pressed — or the overlay died on the
            # way up, and the log below already says why. Either way, stop
            # showing a promise and start showing the truth.
            self._pending = ""

        if self._pending:
            self.status.setStringValue_(self._pending)
            self.status.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        elif running:
            self.status.setStringValue_(self.t("launcher.running"))
            self.status.setTextColor_(AppKit.NSColor.systemGreenColor())
        else:
            self.status.setStringValue_(self.t("launcher.stopped"))
            self.status.setTextColor_(AppKit.NSColor.secondaryLabelColor())

        self.toggle_button.setTitle_(self.t("launcher.stop") if running
                                     else self.t("launcher.start"))
        self._refresh_log()

    def _refresh_log(self) -> None:
        try:
            info = LOG_PATH.stat()
            stamp = (info.st_size, info.st_mtime_ns)
        except OSError:
            stamp = ()
        if stamp == self._log_stamp:
            return
        self._log_stamp = stamp

        body = log_tail() or self.t("launcher.empty_log")
        self.text.setString_(body)
        self.text.scrollRangeToVisible_((len(body), 0))


class _Target(AppKit.NSObject):
    """Button, timer and application-delegate messages for one launcher."""

    def initWithLauncher_(self, launcher):
        self = objc.super(_Target, self).init()
        if self is None:
            return None
        self._launcher = launcher
        return self

    def toggle_(self, sender) -> None:
        self._launcher.toggle()

    def check_(self, sender) -> None:
        self._launcher.check()

    def tick_(self, timer) -> None:
        try:
            self._launcher.tick()
        except Exception as exc:  # a dead poll loop would freeze the status
            signature = f"{type(exc).__name__}: {exc}"
            if signature != getattr(self, "_last_error", None):
                self._last_error = signature
                print("[hsbg] " + self._launcher.t("log.poll_error",
                                                     error=signature),
                      file=sys.stderr)

    def applicationShouldTerminateAfterLastWindowClosed_(self, app) -> bool:
        return True


def main() -> int:
    launcher = Launcher()
    launcher.build()
    AppHelper.runEventLoop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
