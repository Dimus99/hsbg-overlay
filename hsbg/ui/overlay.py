"""The floating windows drawn next to (and over) Hearthstone.

Four separate windows rather than one tall panel, because they have different
jobs and different mouse behaviour:

* **bar**    — always visible, shows the current combat, owns the hide button.
                Accepts clicks.
* **odds**   — win probability, top centre of the game window, combat only.
                Click-through.
* **panels** — collapsible sections. Accepts clicks (collapse) and tracks hover.
* **popup**  — follows the cursor; shows an opponent's board or a tavern
                minion's pool count. Click-through.
"""
from __future__ import annotations

import sys
import time
from typing import Callable, Optional

import AppKit
import objc
from PyObjCTools import AppHelper

from ..config import Settings
from ..i18n import strings
from ..viewmodel import COLLAPSIBLE_SECTIONS, PopupView, ViewModel
from . import render
from .hover import (DEFAULT_ZONES, Hit, LEADERBOARD_SLOTS, MARK_SIZE, hit_test,
                    zones_from_settings)
from .hswindow import (find_hearthstone_window, hearthstone_is_frontmost,
                       hearthstone_is_running, quartz_to_cocoa)

_WEIGHTS = {
    "regular": AppKit.NSFontWeightRegular,
    "semibold": AppKit.NSFontWeightSemibold,
    "bold": AppKit.NSFontWeightBold,
}


def _color(rgba) -> AppKit.NSColor:
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(*rgba)


_IMAGES: dict[str, object] = {}


def _image_cache(path: str):
    """NSImage per file — decoding a card render on every repaint would be
    wasteful, and a hover popup repaints with the cursor."""
    if not path:
        return None
    image = _IMAGES.get(path)
    if image is None:
        image = AppKit.NSImage.alloc().initWithContentsOfFile_(path)
        if image is None:
            return None
        _IMAGES[path] = image
    return image


def _font(size: float, weight: str) -> AppKit.NSFont:
    return AppKit.NSFont.systemFontOfSize_weight_(
        size, _WEIGHTS.get(weight, AppKit.NSFontWeightRegular))


# How far right of the cursor the free-floating popup sits, counted in cursor
# widths so it clears the pointer itself on any cursor size.
POPUP_CURSOR_GAP = 3.0
FALLBACK_CURSOR_WIDTH = 24.0

# Seconds for one there-and-back sweep of the loading slider.
LOADER_PERIOD = 1.6

# How long the overlay survives our own app becoming frontmost. Clicking a
# panel is not supposed to activate us at all (the windows are non-activating),
# but the menu bar item does — and hiding on that would yank the panels away
# mid-click. After that the user has genuinely switched to something else.
FOCUS_GRACE = 1.5

# Visibility asks the window server who is running and who is in front; at the
# cursor poll rate that is worth caching for a couple of frames.
VISIBILITY_TTL = 0.4


_cursor_width_cache: Optional[float] = None


def _cursor_width() -> float:
    """Width of the *visible* pointer, in points.

    ``NSCursor.image().size()`` is the whole 64pt bitmap the system ships, most
    of which is transparent padding — using it directly would push the popup
    several times further than the arrow the user sees. So measure the opaque
    part once and remember it; the arrow does not change size mid-session
    unless the accessibility cursor slider moves.
    """
    global _cursor_width_cache
    if _cursor_width_cache is not None:
        return _cursor_width_cache

    width = FALLBACK_CURSOR_WIDTH
    try:
        image = AppKit.NSCursor.arrowCursor().image()
        rep = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
        pixels, stride = rep.bitmapData(), rep.bytesPerRow()
        columns, rows = rep.pixelsWide(), rep.pixelsHigh()
        alpha = rep.samplesPerPixel() - 1
        step = rep.samplesPerPixel()
        if rep.hasAlpha() and columns > 0:
            opaque = [x for x in range(columns)
                      if max(pixels[x * step + alpha:rows * stride:stride], default=0) > 16]
            if opaque:
                points_per_pixel = float(image.size().width) / columns
                width = (opaque[-1] - opaque[0] + 1) * points_per_pixel
    except (AttributeError, TypeError, ValueError, IndexError):
        width = FALLBACK_CURSOR_WIDTH

    _cursor_width_cache = width if width > 1.0 else FALLBACK_CURSOR_WIDTH
    return _cursor_width_cache


def _fill_crop(image, width: float, height: float):
    """Source rect that fills ``width``x``height`` without squashing the art.

    Card artwork is taller than the square cell it goes into, so the extra
    height is cut rather than squeezed — and cut off the bottom, because that
    is where the empty ground is and the head is what identifies the minion.
    """
    size = image.size()
    if size.width <= 0 or size.height <= 0 or width <= 0 or height <= 0:
        return AppKit.NSZeroRect
    scale = max(width / size.width, height / size.height)
    crop_w = min(size.width, width / scale)
    crop_h = min(size.height, height / scale)
    # NSImage coordinates are bottom-up: 0.82 keeps the crop near the top.
    return AppKit.NSMakeRect((size.width - crop_w) / 2.0,
                             (size.height - crop_h) * 0.82, crop_w, crop_h)


class OverlayView(AppKit.NSView):
    """Executes drawing ops and reports clicks on hotspots."""

    def initWithFrame_(self, frame):
        self = objc.super(OverlayView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._ops = []
        self._hotspots = []
        self._on_click = None
        return self

    def isFlipped(self) -> bool:
        return True

    def setOps_hotspots_(self, ops, hotspots) -> None:
        self._ops = ops
        self._hotspots = hotspots
        self.setNeedsDisplay_(True)

    def setClickHandler_(self, handler) -> None:
        self._on_click = handler

    def hotspotAt_(self, point):
        for key, x, y, w, h in self._hotspots:
            if x <= point.x <= x + w and y <= point.y <= y + h:
                return key
        return None

    def hotspotRectAt_(self, point):
        for key, x, y, w, h in self._hotspots:
            if x <= point.x <= x + w and y <= point.y <= y + h:
                return (x, y, w, h)
        return None

    def mouseDown_(self, event) -> None:
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        key = self.hotspotAt_(point)
        if key and self._on_click is not None:
            self._on_click(key)

    def drawRect_(self, rect) -> None:
        for op in self._ops:
            kind = op[0]
            if kind == "rect":
                _, x, y, w, h, radius, rgba = op
                path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    AppKit.NSMakeRect(x, y, w, h), radius, radius)
                _color(rgba).set()
                path.fill()
            elif kind == "bar":
                _, x, y, w, h, parts = op
                offset = 0.0
                for fraction, rgba in parts:
                    segment = max(0.0, w * float(fraction))
                    if segment <= 0.4:
                        continue
                    path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                        AppKit.NSMakeRect(x + offset, y, segment, h), h / 2, h / 2)
                    _color(rgba).set()
                    path.fill()
                    offset += segment
            elif kind == "image":
                _, x, y, w, h, path = op
                image = _image_cache(path)
                if image is not None:
                    # The view is flipped so the layout can run top-down, but
                    # NSImage still draws bottom-up — mirror it back around the
                    # destination rect or every card lands upside down.
                    context = AppKit.NSGraphicsContext.currentContext()
                    context.saveGraphicsState()
                    transform = AppKit.NSAffineTransform.transform()
                    transform.translateXBy_yBy_(0.0, y + h)
                    transform.scaleXBy_yBy_(1.0, -1.0)
                    transform.concat()
                    image.drawInRect_fromRect_operation_fraction_(
                        AppKit.NSMakeRect(x, 0.0, w, h), _fill_crop(image, w, h),
                        AppKit.NSCompositingOperationSourceOver, 1.0)
                    context.restoreGraphicsState()
            elif kind == "text":
                _, x, y, string, size, weight, rgba, max_width = op
                style = AppKit.NSMutableParagraphStyle.alloc().init()
                style.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
                attributes = {
                    AppKit.NSFontAttributeName: _font(size, weight),
                    AppKit.NSForegroundColorAttributeName: _color(rgba),
                    AppKit.NSParagraphStyleAttributeName: style,
                }
                AppKit.NSString.stringWithString_(string).drawInRect_withAttributes_(
                    AppKit.NSMakeRect(x, y, max_width, size + 6.0), attributes)


class _Window:
    """One borderless, transparent window plus its view."""

    def __init__(self, width: float, opacity: float, click_through: bool):
        rect = AppKit.NSMakeRect(0, 0, width, 10)
        # A non-activating panel takes clicks without pulling focus away from
        # Hearthstone. A plain NSWindow would make our app frontmost, and the
        # overlay would hide itself the moment you clicked it.
        style = (AppKit.NSWindowStyleMaskBorderless
                 | AppKit.NSWindowStyleMaskNonactivatingPanel)
        self.window = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, AppKit.NSBackingStoreBuffered, False)
        self.window.setFloatingPanel_(True)
        self.window.setBecomesKeyOnlyIfNeeded_(True)
        self.window.setHidesOnDeactivate_(False)
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(AppKit.NSColor.clearColor())
        self.window.setHasShadow_(True)
        self.window.setLevel_(AppKit.NSScreenSaverWindowLevel)
        self.window.setIgnoresMouseEvents_(click_through)
        self.window.setAlphaValue_(opacity)
        self.window.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorStationary
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
            | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
        self.view = OverlayView.alloc().initWithFrame_(rect)
        self.window.setContentView_(self.view)
        self._frame: tuple = ()
        self._shown = False

    def apply(self, ops, size, hotspots, origin: tuple[float, float],
              visible: bool) -> None:
        width, height = size
        if not visible or height <= 0.5:
            if self._shown:
                self.window.orderOut_(None)
                self._shown = False
            return
        self.view.setOps_hotspots_(ops, hotspots)
        frame = (origin[0], origin[1], width, height)
        if frame != self._frame:
            self.window.setFrame_display_(
                AppKit.NSMakeRect(origin[0], origin[1], width, height), True)
            self.view.setFrame_(AppKit.NSMakeRect(0, 0, width, height))
            self._frame = frame
        if not self._shown:
            self.window.orderFrontRegardless()
            self._shown = True

    def contains(self, x: float, y: float) -> bool:
        if not self._shown or not self._frame:
            return False
        fx, fy, fw, fh = self._frame
        return fx <= x <= fx + fw and fy <= y <= fy + fh


class OverlayController:
    """Owns the windows, keeps them glued to Hearthstone, renders updates."""

    def __init__(self, settings: Settings, on_quit: Optional[Callable[[], None]] = None):
        self.settings = settings
        self.on_quit = on_quit
        # Menu titles and the labels we draw follow the game's own language.
        language = settings.resolved_language()
        self.t = strings(language)
        self.model = ViewModel(language=language)
        self.model.hidden = bool(settings.extra.get("hidden", False))
        # Row hotspots used to be saved here as if they were sections; drop
        # anything that is not one so old settings files clean themselves up.
        self.model.collapsed = {
            key: bool(value)
            for key, value in dict(settings.extra.get("collapsed", {})).items()
            if key in COLLAPSIBLE_SECTIONS}
        self.zones = zones_from_settings(settings.extra)
        self.recompute_hook: Optional[Callable[[], None]] = None
        # Called with the new state when the debug journal is toggled; returns
        # the directory it writes to, so the menu can say where to look.
        self.debug_log_hook: Optional[Callable[[bool], object]] = None
        # Filled in by the app so hover can be resolved without the game logic
        # reaching into the UI.
        self.resolve_hover: Optional[Callable[[Optional[Hit], Optional[str]],
                                              Optional[PopupView]]] = None
        self._built = False
        self._hover_key: Optional[str] = None
        self._hit: Optional[Hit] = None
        self._hs_frontmost_at = 0.0
        self._visible_cache = False
        self._visible_at = 0.0
        self._was_visible = False
        # Screen position to dock the popup to; None means follow the cursor.
        self._popup_anchor: Optional[tuple[float, float]] = None

    # ------------------------------------------------------------------ build

    def build(self) -> None:
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

        scale = self.settings.overlay_scale
        opacity = float(self.settings.overlay_opacity)
        self.bar = _Window(render.BAR_WIDTH * scale, opacity, click_through=False)
        self.panels = _Window(render.PANEL_WIDTH * scale, opacity, click_through=False)
        self.odds = _Window(render.ODDS_WIDTH * scale, opacity, click_through=True)
        self.popup = _Window(render.POPUP_WIDTH * scale, opacity, click_through=True)
        # Sits on the game's own portrait rail; must never eat a click meant
        # for Hearthstone, hence click-through like the popup.
        self.marks = _Window(MARK_SIZE * 2, opacity, click_through=True)

        self.bar.view.setClickHandler_(self._on_click)
        self.panels.view.setClickHandler_(self._on_click)

        self._built = True
        self._install_status_item()
        self._start_timer()
        self.refresh()

    def _install_status_item(self) -> None:
        bar = AppKit.NSStatusBar.systemStatusBar()
        self.status_item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        self.status_item.button().setTitle_("BG")
        menu = AppKit.NSMenu.alloc().init()
        self._menu_target = _MenuTarget.alloc().initWithController_(self)
        entries = ((self.t("menu.toggle_hidden"), "toggleHidden_"),
                   (self.t("menu.marks"), "toggleMarks_"),
                   (self.t("menu.recompute"), "recompute_"),
                   (None, None),
                   (self.t("menu.debug_log"), "toggleDebugLog_"),
                   (self.t("menu.debug_folder"), "openDebugFolder_"),
                   (None, None),
                   (self.t("menu.quit"), "quitApp_"))
        for title, selector in entries:
            if title is None:
                menu.addItem_(AppKit.NSMenuItem.separatorItem())
                continue
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, objc.selector(getattr(_MenuTarget, selector), signature=b"v@:@"), "")
            item.setTarget_(self._menu_target)
            menu.addItem_(item)
            if selector == "toggleMarks_":
                # The panel switch is the primary one, but it only exists while
                # a lobby table is on screen — this one always is.
                self._marks_item = item
            elif selector == "toggleDebugLog_":
                self._debug_item = item
        self.status_item.setMenu_(menu)
        self._sync_marks()
        self._sync_debug()

    def _start_timer(self) -> None:
        """Poll the cursor: macOS gives no hover events for other apps' windows."""
        self._timer_target = _TimerTarget.alloc().initWithController_(self)
        self.timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.12, self._timer_target, objc.selector(_TimerTarget.tick_, signature=b"v@:@"),
            None, True)
        AppKit.NSRunLoop.currentRunLoop().addTimer_forMode_(
            self.timer, AppKit.NSRunLoopCommonModes)

    # ---------------------------------------------------------------- updates

    def update(self, model: ViewModel) -> None:
        """Safe to call from any thread."""
        model.hidden = self.model.hidden
        model.collapsed = self.model.collapsed
        model.popup = self.model.popup
        model.hover_key = self.model.hover_key
        self.model = model
        AppHelper.callAfter(self.refresh)

    def _visible(self) -> bool:
        """Whether anything of ours belongs on screen right now.

        This is a Hearthstone overlay: with the game closed there is nothing to
        sit on top of, and a win-probability card left floating over the desktop
        is worse than no card at all.
        """
        now = time.monotonic()
        if now - self._visible_at < VISIBILITY_TTL:
            return self._visible_cache
        self._visible_at = now
        self._visible_cache = self._compute_visible(now)
        return self._visible_cache

    def _compute_visible(self, now: float) -> bool:
        if self.settings.show_when_hs_inactive:
            return True
        # Cheap check first — the running-apps sweep is the expensive one.
        if hearthstone_is_frontmost():
            self._hs_frontmost_at = now
            return True
        if not hearthstone_is_running():
            return False
        if AppKit.NSApplication.sharedApplication().isActive():
            return now - self._hs_frontmost_at <= FOCUS_GRACE
        return False

    @staticmethod
    def _phase() -> float:
        """Position in the loader's animation cycle, 0..1."""
        return (time.monotonic() % LOADER_PERIOD) / LOADER_PERIOD

    def _odds_visible(self, rect, size) -> bool:
        """The odds card only makes sense pinned over the game window.

        Without a window to pin to, ``_anchor`` falls back to the screen and the
        card ends up parked over the middle of the desktop — which is what it
        looked like when it "hung there" after the game was gone.
        """
        if not self.model.in_combat or size[1] <= 0:
            return False
        return rect is not None or self.settings.show_when_hs_inactive

    def _odds_frame(self, rect, size) -> tuple[float, float]:
        """Odds sit at the top centre of the game window, where the eye already is."""
        anchor_x, anchor_y, game_w, _ = self._anchor(rect)
        return (anchor_x + (game_w - size[0]) / 2.0 - self.settings.offset_x,
                anchor_y - size[1] - 4.0)

    def refresh_odds(self) -> None:
        """Repaint just the odds card — used to animate the loader."""
        if not self._built:
            return
        ops, size, _ = render.build_odds(
            self.model, render.ODDS_WIDTH * self.settings.overlay_scale, self._phase())
        rect = find_hearthstone_window()
        self.odds.apply(ops, size, [], self._odds_frame(rect, size),
                        self._visible() and self._odds_visible(rect, size))

    def _sync_marks(self) -> None:
        """Mirror the pins setting into the two places that display it."""
        enabled = bool(self.settings.show_hover_marks)
        self.model.show_marks = enabled
        item = getattr(self, "_marks_item", None)
        if item is not None:
            item.setState_(AppKit.NSControlStateValueOn if enabled
                           else AppKit.NSControlStateValueOff)

    def _sync_debug(self) -> None:
        """Show the journal's state as a checkmark on its menu item."""
        item = getattr(self, "_debug_item", None)
        if item is not None:
            item.setState_(AppKit.NSControlStateValueOn
                           if self.settings.debug_log else AppKit.NSControlStateValueOff)

    def toggle_debug_log(self) -> None:
        hook = self.debug_log_hook
        enabled = not self.settings.debug_log
        directory = hook(enabled) if hook is not None else None
        self.settings.debug_log = enabled
        self._sync_debug()
        # The journal is invisible by design, so say where it went — otherwise
        # switching it on gives no sign that anything happened.
        where = f" → {directory}" if enabled and directory else ""
        print("[hsbg] " + self.t("log.journal_on" if enabled else "log.journal_off")
              + where, file=sys.stderr)

    def open_debug_folder(self) -> None:
        from ..config import DEBUG_DIR
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        AppKit.NSWorkspace.sharedWorkspace().openFile_(str(DEBUG_DIR))

    def refresh(self) -> None:
        if not self._built:
            return
        self._sync_marks()
        self._sync_debug()
        rect = find_hearthstone_window()
        visible = self._visible()

        scale = self.settings.overlay_scale
        bar_ops, bar_size, bar_spots = render.build_main_bar(
            self.model, render.BAR_WIDTH * scale)
        panel_ops, panel_size, panel_spots = render.build_panels(
            self.model, render.PANEL_WIDTH * scale)
        odds_ops, odds_size, _ = render.build_odds(
            self.model, render.ODDS_WIDTH * scale, self._phase())
        popup_ops, popup_size, _ = render.build_popup(
            self.model.popup, render.POPUP_WIDTH * scale)

        anchor_x, anchor_y, game_w, game_h = self._anchor(rect)

        bar_origin = (anchor_x, anchor_y - bar_size[1])
        self.bar.apply(bar_ops, bar_size, bar_spots, bar_origin, visible)

        panel_origin = (anchor_x, bar_origin[1] - 6.0 - panel_size[1])
        self.panels.apply(panel_ops, panel_size, panel_spots, panel_origin,
                          visible and not self.model.hidden)

        self.odds.apply(odds_ops, odds_size, [], self._odds_frame(rect, odds_size),
                        visible and self._odds_visible(rect, odds_size))

        self.popup.apply(popup_ops, popup_size, [], self._popup_origin(popup_size),
                         visible and popup_size[1] > 0)

        self._refresh_marks(rect, visible)

    def _marks_active(self) -> bool:
        """Are the "?" pins actually on screen right now?

        The pins are the only hotspot on the opponent rail, so this is also the
        gate for hit-testing it: with them switched off, sweeping the cursor
        past the portraits must do nothing at all.

        Collapsing is deliberately *not* one of the conditions. The pins are a
        separate overlay on the game's own rail with a switch of their own, and
        collapsing is how you get the panels out of the way while still reading
        opponents' boards — taking the pins down with the panels removed the one
        thing collapsing is for, and left no way to bring them back either,
        since their switch lives in the lobby table that just went away.
        """
        return bool(self.settings.show_hover_marks and self.model.connected
                    and self.model.turn >= 1)

    def _refresh_marks(self, rect, visible: bool) -> None:
        """The "?" pins over Hearthstone's opponent rail."""
        frame = self._rail_frame(rect)
        if frame is None:
            self.marks.apply([], (0.0, 0.0), [], (0.0, 0.0), False)
            return
        x, y, width, height = frame
        hover = self._hit.index if (self._hit is not None
                                    and self._hit.kind == "hero") else -1
        ops, size, _ = render.build_hero_marks(self.model.leaderboard, width, height,
                                               slots=LEADERBOARD_SLOTS,
                                               hover_index=hover)
        self.marks.apply(ops, (width, size[1]), [], (x, y),
                         visible and self._marks_active() and size[1] > 0)

    def _rail_frame(self, rect) -> Optional[tuple[float, float, float, float]]:
        """The opponent rail in Cocoa coordinates: (x, y, width, height).

        Straight from the hover zone, so the pins cannot drift away from the
        bands that actually trigger the popup.
        """
        if rect is None:
            return None
        left, top, right, bottom = self.zones.get("leaderboard",
                                                  DEFAULT_ZONES["leaderboard"])
        gx, gy, gw, gh = quartz_to_cocoa(rect)
        width, height = (right - left) * gw, (bottom - top) * gh
        if width <= 0 or height <= 0:
            return None
        # ``top``/``bottom`` count down from the window's top edge; Cocoa counts up.
        return gx + left * gw, gy + gh - bottom * gh, width, height

    def _anchor(self, rect) -> tuple[float, float, float, float]:
        """Top-left of our column, in Cocoa coordinates, plus the game size."""
        if rect is None:
            screen = AppKit.NSScreen.mainScreen().frame()
            return (40.0, screen.size.height - 60.0, screen.size.width, screen.size.height)
        gx, gy, gw, gh = quartz_to_cocoa(rect)
        x = gx + self.settings.offset_x
        if self.settings.anchor.endswith("right"):
            x = gx + gw - render.PANEL_WIDTH * self.settings.overlay_scale \
                - self.settings.offset_x
        return x, gy + gh - self.settings.offset_y, gw, gh

    def _popup_origin(self, size) -> tuple[float, float]:
        screen = AppKit.NSScreen.mainScreen().frame()
        if self._popup_anchor is not None:
            x, top = self._popup_anchor
            y = top - size[1]
        else:
            location = AppKit.NSEvent.mouseLocation()
            x = location.x + POPUP_CURSOR_GAP * _cursor_width()
            y = location.y - size[1] - 12.0
        x = max(4.0, min(x, screen.size.width - size[0] - 4.0))
        y = max(4.0, min(y, screen.size.height - size[1] - 4.0))
        return x, y

    # ------------------------------------------------------------------ mouse

    def tick(self) -> None:
        """Cursor poll: resolve what is being hovered and repaint if it changed."""
        if not self._built or self.resolve_hover is None:
            return

        visible = self._visible()
        if not visible:
            # Nothing of ours is on screen. Repaint once so the windows go away
            # without waiting for the next simulation tick, drop the hover state
            # so no popup flashes back when the game returns, and skip the
            # hit-testing entirely — there is nothing to point at.
            if self._was_visible:
                self._was_visible = False
                self._hover_key = None
                self._hit = None
                self.model.popup = None
                self.model.hover_key = None
                self.refresh()
            return
        if not self._was_visible:
            self._was_visible = True
            self.refresh()

        location = AppKit.NSEvent.mouseLocation()

        panel_key = None
        anchor = None
        if self.panels.contains(location.x, location.y):
            point = self.panels.window.convertPointFromScreen_(location)
            view_point = self.panels.view.convertPoint_fromView_(point, None)
            panel_key = self.panels.view.hotspotAt_(view_point)
            spot = self.panels.view.hotspotRectAt_(view_point)
            if spot is not None and self.panels._frame:
                # Dock beside the row that is being pointed at, so the popup
                # never lands under the cursor or on top of the panel itself.
                px, py, pw, ph = self.panels._frame
                row_x, row_y, _, row_h = spot
                anchor = (px + pw + 8.0, py + ph - row_y - row_h)

        hit = None
        # Without a match on screen the zones point at menu artwork, and the
        # boards behind them belong to a lobby that is already over.
        if panel_key is None and self.model.connected:
            rect = find_hearthstone_window()
            if rect is not None:
                screen = AppKit.NSScreen.screens()[0].frame() if AppKit.NSScreen.screens() else None
                if screen is not None:
                    # NSEvent gives bottom-left origin; the window list is top-left.
                    quartz = (location.x, screen.size.height - location.y)
                    hit = hit_test(quartz, rect, self.zones,
                                   tavern_slots=self._tavern_slots,
                                   marks_enabled=self._marks_active())

        key = panel_key or (f"{hit.kind}:{hit.index}" if hit else None)
        if key == self._hover_key:
            popup = self.model.popup
            if popup is not None and any(not m.image for m in popup.minions):
                # Card renders arrive asynchronously; rebuild so they appear.
                self.model.popup = self.resolve_hover(hit, panel_key)
                self.refresh()
                return
            if self.model.odds_pending:
                # Nothing else is repainting while we wait for the numbers, so
                # this tick is what keeps the loader moving.
                self.refresh_odds()
            if self.model.popup is not None:
                # Keep the popup glued to the moving cursor.
                _, size, _ = render.build_popup(self.model.popup,
                                                render.POPUP_WIDTH * self.settings.overlay_scale)
                self.popup.apply(*render.build_popup(
                    self.model.popup, render.POPUP_WIDTH * self.settings.overlay_scale),
                    self._popup_origin(size), True)
            return
        self._hover_key = key
        self._hit = hit
        self._popup_anchor = anchor
        # Panels highlight the row under the cursor, so the connection between
        # the row and the board that pops up beside it is visible.
        self.model.hover_key = panel_key
        self.model.popup = self.resolve_hover(hit, panel_key)
        self.refresh()

    @property
    def _tavern_slots(self) -> int:
        return getattr(self, "tavern_slots", 0)

    def set_tavern_slots(self, count: int) -> None:
        self.tavern_slots = count

    def _on_click(self, key: str) -> None:
        if key == render.KEY_HIDE:
            self.model.hidden = not self.model.hidden
            self.settings.extra["hidden"] = self.model.hidden
        elif key == render.KEY_MARKS:
            self.settings.show_hover_marks = not self.settings.show_hover_marks
            self._sync_marks()
        elif key in COLLAPSIBLE_SECTIONS:
            self.model.collapsed[key] = not self.model.collapsed.get(key, False)
            self.settings.extra["collapsed"] = self.model.collapsed
        else:
            # An opponent row: it exists to be hovered, not clicked.
            return
        try:
            self.settings.save()
        except OSError:
            pass
        self.refresh()

    # ------------------------------------------------------------------ misc

    def toggle(self) -> None:
        self._on_click(render.KEY_HIDE)

    def toggle_marks(self) -> None:
        self._on_click(render.KEY_MARKS)

    def quit(self) -> None:
        if self.on_quit is not None:
            self.on_quit()
        AppKit.NSApplication.sharedApplication().terminate_(None)


class _MenuTarget(AppKit.NSObject):
    def initWithController_(self, controller):
        self = objc.super(_MenuTarget, self).init()
        if self is None:
            return None
        self._controller = controller
        return self

    def toggleHidden_(self, sender) -> None:
        self._controller.toggle()

    def toggleMarks_(self, sender) -> None:
        self._controller.toggle_marks()

    def recompute_(self, sender) -> None:
        hook = self._controller.recompute_hook
        if hook is not None:
            hook()

    def toggleDebugLog_(self, sender) -> None:
        self._controller.toggle_debug_log()

    def openDebugFolder_(self, sender) -> None:
        self._controller.open_debug_folder()

    def quitApp_(self, sender) -> None:
        self._controller.quit()


class _TimerTarget(AppKit.NSObject):
    def initWithController_(self, controller):
        self = objc.super(_TimerTarget, self).init()
        if self is None:
            return None
        self._controller = controller
        return self

    def tick_(self, timer) -> None:
        try:
            self._controller.tick()
        except Exception as exc:
            # Report once: a silently broken hover loop is worse than one line
            # of noise, and repeating it 8x a second would be unusable.
            signature = f"{type(exc).__name__}: {exc}"
            if signature != getattr(self, "_last_error", None):
                self._last_error = signature
                print("[hsbg] " + self._controller.t("log.hover_error",
                                                       error=signature),
                      file=sys.stderr)
