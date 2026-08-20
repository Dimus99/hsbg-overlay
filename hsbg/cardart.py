"""Card renders for the hover popup, fetched lazily and cached on disk.

HearthstoneJSON serves the raw card artwork, which we frame ourselves. The
finished, framed renders under ``/render/latest/`` were the obvious choice but
they only cover older sets — 11 of 13 minions from a current Battlegrounds match
404 there — so a mix of framed and unframed cards would look broken. The plain
art endpoint has every card, and drawing our own frame also lets the live
attack/health sit where they belong.

Downloads happen on a background thread so a popup never blocks on the network:
the first hover over an unseen card draws a placeholder, and the picture appears
a moment later. Golden cards share the base card's art and get a gold border.
"""
from __future__ import annotations

import queue
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .config import CACHE_DIR

ART_URL = "https://art.hearthstonejson.com/v1/256x/{card_id}.jpg"
# Bump when the endpoint changes so old, differently-shaped files are ignored
# instead of leaving the popup a mix of two styles.
CACHE_VERSION = "v3-art256-trim"
USER_AGENT = "hsbg-overlay/1.0 (+local Battlegrounds overlay)"
MAX_PENDING = 64
# A line of pixels this bright on every channel counts as padding. The bar is
# below pure white on purpose: JPEG ringing fades the last few rows of padding
# into grey, and real artwork practically never has a full row that pale.
WHITE = 228
HALO = 1


class ArtCache:
    def __init__(self, locale: str = "enUS", offline: bool = False):
        # Art is language-independent; the locale only names the cache folder.
        self.locale = locale
        self.offline = offline
        self.dir = CACHE_DIR / "art" / CACHE_VERSION
        self._queue: "queue.Queue[str]" = queue.Queue(maxsize=MAX_PENDING)
        self._requested: set[str] = set()
        self._missing: set[str] = set()
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._on_ready = None

    def start(self, on_ready=None) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._drop_stale_versions()
        self._on_ready = on_ready
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, name="hsbg-art", daemon=True)
            self._worker.start()

    # ------------------------------------------------------------------

    def _drop_stale_versions(self) -> None:
        root = self.dir.parent
        try:
            for child in root.iterdir():
                if child.is_dir() and child.name != CACHE_VERSION:
                    for item in child.iterdir():
                        item.unlink(missing_ok=True)
                    child.rmdir()
        except OSError:
            pass

    def _file(self, card_id: str) -> Path:
        # The CDN serves JPEG and the trim below re-encodes as JPEG, so the
        # cache keeps a single format end to end.
        return self.dir / f"{card_id}.jpg"

    def path_for(self, card_id: str) -> Optional[str]:
        """Local file for this card, or None while it is still being fetched."""
        if not card_id:
            return None
        base = card_id[:-2] if card_id.endswith("_G") else card_id
        target = self._file(base)
        if target.exists():
            return str(target)
        if self.offline or base in self._missing:
            return None
        with self._lock:
            if base in self._requested:
                return None
            self._requested.add(base)
        try:
            self._queue.put_nowait(base)
        except queue.Full:
            with self._lock:
                self._requested.discard(base)
        return None

    def _run(self) -> None:
        while True:
            card_id = self._queue.get()
            try:
                self._download(card_id)
            except Exception:
                self._missing.add(card_id)
            finally:
                with self._lock:
                    self._requested.discard(card_id)
                self._queue.task_done()

    def _download(self, card_id: str) -> None:
        target = self._file(card_id)
        if target.exists():
            return
        url = ART_URL.format(card_id=card_id)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = response.read()
        except urllib.error.HTTPError:
            self._missing.add(card_id)
            return
        tmp = target.with_suffix(".part")
        tmp.write_bytes(_trim_padding(payload))
        tmp.replace(target)
        if self._on_ready is not None:
            self._on_ready(card_id)

    def prefetch(self, card_ids) -> None:
        for card_id in card_ids:
            self.path_for(card_id)


# --------------------------------------------------------------------------
# padding trim
# --------------------------------------------------------------------------

def _trim_padding(data: bytes) -> bytes:
    """Crop the white letterbox the CDN pads portrait artwork with.

    The 256x endpoint returns a square for every card, so anything that is not
    square gets white bars — which looked like a rendering bug in the popup.
    Cropping once, on download, keeps the drawing code trivial.
    """
    try:
        import AppKit
    except ImportError:                       # headless use of the cache
        return data

    # This runs on the download thread, which has no pool of its own; without
    # one every autoreleased bitmap would leak and log about it.
    pool = AppKit.NSAutoreleasePool.alloc().init()
    try:
        return _trim(data, AppKit)
    finally:
        del pool


def _trim(data: bytes, AppKit) -> bytes:
    source = AppKit.NSBitmapImageRep.imageRepWithData_(
        AppKit.NSData.dataWithBytes_length_(data, len(data)))
    if source is None:
        return data
    width, height = source.pixelsWide(), source.pixelsHigh()
    if width <= 0 or height <= 0:
        return data

    canvas = _rgba_copy(source, width, height)
    if canvas is None:
        return data
    pixels = canvas.bitmapData()
    stride = canvas.bytesPerRow()

    def row_is_padding(y: int) -> bool:
        row = pixels[y * stride:y * stride + width * 4]
        return all(min(row[channel::4]) >= WHITE for channel in (0, 1, 2))

    def column_is_padding(x: int) -> bool:
        base = x * 4
        return all(min(pixels[base + channel:height * stride:stride]) >= WHITE
                   for channel in (0, 1, 2))

    top, bottom = 0, height - 1
    while top < bottom and row_is_padding(top):
        top += 1
    while bottom > top and row_is_padding(bottom):
        bottom -= 1
    left, right = 0, width - 1
    while left < right and column_is_padding(left):
        left += 1
    while right > left and column_is_padding(right):
        right -= 1

    # JPEG ringing leaves a pale halo where the padding met the artwork, just
    # dark enough to read as content. Shave it off wherever padding was found.
    if left > 0:
        left = min(left + HALO, right)
    if right < width - 1:
        right = max(right - HALO, left)
    if top > 0:
        top = min(top + HALO, bottom)
    if bottom < height - 1:
        bottom = max(bottom - HALO, top)

    crop_w, crop_h = right - left + 1, bottom - top + 1
    if crop_w < 16 or crop_h < 16 or (crop_w == width and crop_h == height):
        return data

    cropped = _crop(canvas, left, top, crop_w, crop_h)
    if cropped is None:
        return data
    # Re-encode as JPEG: PNG would be ~9x the bytes for the same picture.
    encoded = cropped.representationUsingType_properties_(
        AppKit.NSBitmapImageFileTypeJPEG,
        {AppKit.NSImageCompressionFactor: 0.9})
    return bytes(encoded) if encoded is not None else data


def _rgba_copy(rep, width: int, height: int):
    """Redraw a rep into a plain 8-bit RGBA bitmap we can read byte-wise."""
    import AppKit

    out = AppKit.NSBitmapImageRep.alloc() \
        .initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, width, height, 8, 4, True, False,
            AppKit.NSDeviceRGBColorSpace, width * 4, 32)
    if out is None:
        return None
    context = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(out)
    if context is None:
        return None
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(context)
    rep.drawInRect_(AppKit.NSMakeRect(0, 0, width, height))
    AppKit.NSGraphicsContext.restoreGraphicsState()
    return out


def _crop(rep, x: int, y: int, width: int, height: int):
    """Cut a pixel rect out of a bitmap.

    Done through CoreGraphics rather than by redrawing: ``NSBitmapImageRep``
    ignores the source rect when it draws into a bitmap context, which silently
    produced a squashed copy of the whole image instead of a crop.
    """
    import AppKit
    import Quartz

    image = rep.CGImage()
    if image is None:
        return None
    # CGImage rects are top-left based, same as the scan above.
    cut = Quartz.CGImageCreateWithImageInRect(
        image, Quartz.CGRectMake(x, y, width, height))
    if cut is None:
        return None
    return AppKit.NSBitmapImageRep.alloc().initWithCGImage_(cut)
