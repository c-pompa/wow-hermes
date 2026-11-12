"""Finding the WoW window and grabbing the strip's search region (PROTOCOL E compliant).

Four rules shape this module:

* **No process access.** The window is found by class name + title via ``EnumWindows``. We
  never call ``OpenProcess``, never read memory, never send input. ``GetWindowThreadProcessId``
  is deliberately *not* used to filter by image name, because doing that properly needs a
  process handle.
* **Never assume a pitch.** The capture layer returns pixels; :mod:`decoder` decides the pitch.
  The one thing it *does* report is the client size, so the caller can turn the hello's
  ``g=WxH`` (the render resolution) into an anisotropic pitch hint.
* **The window is not a constant.** Players alt-tab, drag WoW to another monitor and switch
  between fullscreen and windowed, so the client rect is re-resolved every
  :data:`GEOMETRY_REFRESH` seconds and the grab region is rebuilt when it changes.
* **DPI awareness first.** ``SetProcessDpiAwarenessContext(-4)`` must succeed *before* any
  window call, or ``GetClientRect`` returns logical pixels while WGC returns physical ones.

WGC (``windows-capture`` 2.x) is the primary source because it keeps working while the overlay
has focus; GDI (``mss``) over the composed desktop is the fallback. Microsoft's exclusive
fullscreen defeats both — that is a documented, user-visible failure.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Protocol

import numpy as np

IS_WINDOWS = sys.platform == "win32"

#: Window classes Forever has used across builds (research doc 06 §6.4 note + prior art).
WOW_CLASSES = ("GxWindowClassD3d", "GxWindowClass", "waApplication Window")
WOW_TITLE = "World of Warcraft"

#: PROTOCOL A.3 (rev 2026-11-08): the capture search region is the top-left 1600x400
#: physical pixels of the client area. That covers the widest strip (pitch 2.0 = 1600 px)
#: and the tallest one (48 rows * 4 * 2 = 384 px). The decoder is size-agnostic.
SEARCH_REGION = (1600, 400)
NARROW = SEARCH_REGION
#: Kept as an alias for `probe-capture --wide` compatibility; same region now.
WIDE = SEARCH_REGION

#: How often the client rect is re-resolved (in-game round 1: the bridge cached the geometry
#: once and kept grabbing a stale region after the window moved).
GEOMETRY_REFRESH = 2.0

MINIMISED_MESSAGE = "WoW is minimised: restore the window to send messages"


class CaptureError(RuntimeError):
    """Capture failed for a reason the player can act on."""


class NoWowWindow(CaptureError):
    """No WoW window is on screen (game closed, or exclusive fullscreen)."""


class Capture(Protocol):
    def grab(self) -> np.ndarray: ...

    def describe(self) -> str: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str


@dataclass(frozen=True)
class Geometry:
    """The client area of the WoW window in physical screen pixels."""

    hwnd: int
    left: int
    top: int
    width: int
    height: int
    title: str = WOW_TITLE
    class_name: str = ""
    dpi: int = 96

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.width, self.height

    def describe(self) -> str:
        return f"client {self.width}x{self.height} at {self.left},{self.top} (hwnd {self.hwnd}, dpi {self.dpi})"


# ---------------------------------------------------------------- DPI


_DPI_SETTLED = False
_DPI_STATE = "unknown"


def dpi_state() -> str:
    """What :func:`enable_dpi_awareness` achieved: ``per-monitor-v2`` is what we need."""
    return _DPI_STATE


def enable_dpi_awareness() -> bool:
    """PER_MONITOR_AWARE_V2 before any window is created; idempotent, never raises.

    Returns True only for per-monitor (v2, or v1 via shcore) awareness — the only modes in
    which ``GetClientRect`` and WGC agree on physical pixels.
    """
    global _DPI_SETTLED, _DPI_STATE
    if _DPI_SETTLED:
        return _DPI_STATE in ("per-monitor-v2", "per-monitor")
    _DPI_SETTLED = True
    if not IS_WINDOWS:
        _DPI_STATE = "not-windows"
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        setter = user32.SetProcessDpiAwarenessContext
        setter.restype = wintypes.BOOL
        setter.argtypes = [ctypes.c_void_p]
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == (HANDLE)-4
        if setter(ctypes.c_void_p(-4)):
            _DPI_STATE = "per-monitor-v2"
            return True
        error = ctypes.get_last_error()
        # ERROR_ACCESS_DENIED (5) means the host process already set a context; whatever it
        # is, we cannot upgrade it from here.
        _DPI_STATE = "already-set" if error == 5 else f"failed(error {error})"
        return False
    except Exception as exc:  # noqa: BLE001 - older Windows / no export
        _DPI_STATE = f"unavailable ({exc})"
    try:
        import ctypes

        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        _DPI_STATE = "per-monitor"
        return True
    except Exception as exc:  # noqa: BLE001
        _DPI_STATE = f"none ({exc})"
        return False


def window_dpi(hwnd: int) -> int:
    """Per-window DPI (96 = 100 %); best effort, never raises."""
    if not IS_WINDOWS:
        return 96
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")
        getter = user32.GetDpiForWindow
        getter.restype = wintypes.UINT
        getter.argtypes = [wintypes.HWND]
        return int(getter(wintypes.HWND(hwnd)) or 96)
    except Exception:  # noqa: BLE001 - pre-1607 Windows
        return 96


# ---------------------------------------------------------------- window finder


def _enum_windows() -> list[WindowInfo]:
    if not IS_WINDOWS:
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[WindowInfo] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):  # noqa: ANN001
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value
        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, 256)
        class_name = class_buffer.value
        if class_name in WOW_CLASSES or title.startswith(WOW_TITLE):
            found.append(WindowInfo(hwnd=int(hwnd), title=title, class_name=class_name))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def find_wow_window() -> WindowInfo | None:
    """The visible WoW window, or None. Case-insensitive on the title, exact on the class."""
    windows = _enum_windows()
    for window in windows:
        if window.class_name in WOW_CLASSES:
            return window
    for window in windows:
        if window.title.lower().startswith(WOW_TITLE.lower()):
            return window
    return None


def client_rect(hwnd: int) -> tuple[int, int, int, int]:
    """Client area in screen coordinates ``(left, top, width, height)`` — not the window rect."""
    if not IS_WINDOWS:
        raise CaptureError("client_rect is Windows-only")
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise CaptureError(f"GetClientRect failed for hwnd {hwnd}")
    point = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(point)):
        raise CaptureError(f"ClientToScreen failed for hwnd {hwnd}")
    return point.x, point.y, rect.right - rect.left, rect.bottom - rect.top


def is_minimized(hwnd: int) -> bool:
    if not IS_WINDOWS:
        return False
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    return bool(user32.IsIconic(hwnd))


def window_alive(hwnd: int) -> bool:
    """``IsWindow``: a recycled handle would still have a class, so check validity, not identity."""
    if not IS_WINDOWS:
        return False
    import ctypes

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        return bool(user32.IsWindow(hwnd))
    except Exception:  # noqa: BLE001
        return False


def resolve_geometry(window: WindowInfo | None = None) -> Geometry:
    """The current client geometry of the WoW window; raises :class:`NoWowWindow` if it is gone."""
    window = window or find_wow_window()
    if window is None:
        raise NoWowWindow("no WoW window")
    if is_minimized(window.hwnd):
        raise NoWowWindow(MINIMISED_MESSAGE)
    left, top, width, height = client_rect(window.hwnd)
    return Geometry(
        hwnd=window.hwnd,
        left=left,
        top=top,
        width=width,
        height=height,
        title=window.title,
        class_name=window.class_name,
        dpi=window_dpi(window.hwnd),
    )


def search_region(rect: tuple[int, int, int, int], size: tuple[int, int] = SEARCH_REGION) -> dict[str, int]:
    """Clip the requested region to the client area, anchored at its top-left corner."""
    left, top, width, height = rect
    return {
        "left": left,
        "top": top,
        "width": max(1, min(size[0], width)),
        "height": max(1, min(size[1], height)),
    }


class _GeometryWatcher:
    """Re-resolve the tracked window's client rect every ``refresh_interval`` seconds.

    Shared by both capture sources. ``poll()`` returns the (possibly updated) geometry and
    raises :class:`NoWowWindow` when the window is minimised, dead or replaced — the app's
    capture factory then re-acquires, which is what makes an alt-tab or a window drag recover.
    """

    def __init__(
        self,
        geometry: Geometry | None,
        *,
        refresh_interval: float = GEOMETRY_REFRESH,
        clock: Callable[[], float] = time.monotonic,
        on_geometry: Callable[[Geometry], None] | None = None,
    ):
        self.geometry = geometry
        self.refresh_interval = refresh_interval
        self.clock = clock
        self.on_geometry = on_geometry
        self._next_probe = clock() + refresh_interval

    def poll(self, *, force: bool = False) -> Geometry | None:
        if self.geometry is None:  # legacy: an explicit region, nothing to follow
            return None
        now = self.clock()
        if not force and now < self._next_probe:
            return self.geometry
        self._next_probe = now + self.refresh_interval

        tracked = self.geometry.hwnd
        if not window_alive(tracked):
            window = find_wow_window()
            if window is None:
                raise NoWowWindow("no WoW window")
            raise NoWowWindow(f"WoW window changed (hwnd {tracked} -> {window.hwnd}); re-acquiring")
        if is_minimized(tracked):
            raise NoWowWindow(MINIMISED_MESSAGE)
        left, top, width, height = client_rect(tracked)
        if (left, top, width, height) != self.geometry.rect:
            self.geometry = replace(self.geometry, left=left, top=top, width=width, height=height)
            if self.on_geometry is not None:
                try:
                    self.on_geometry(self.geometry)
                except Exception:  # noqa: BLE001 - a callback must never break capture
                    pass
        return self.geometry


class _WindowBound:
    """Common surface for capture sources that follow a live WoW window."""

    _watcher: _GeometryWatcher

    @property
    def on_geometry(self) -> Callable[[Geometry], None] | None:
        return self._watcher.on_geometry

    @on_geometry.setter
    def on_geometry(self, callback: Callable[[Geometry], None] | None) -> None:
        self._watcher.on_geometry = callback

    @property
    def geometry(self) -> Geometry | None:
        return self._watcher.geometry

    def client_size(self) -> tuple[int, int] | None:
        """Current client size (physical px) — the decoder turns this into a pitch hint."""
        geometry = self._watcher.geometry
        return None if geometry is None else (geometry.width, geometry.height)

    def refresh(self, *, force: bool = False) -> Geometry | None:
        """Re-resolve the window geometry (throttled to ``refresh_interval`` unless forced)."""
        return self._watcher.poll(force=force)


# ---------------------------------------------------------------- GDI (mss)


class GdiCapture(_WindowBound):
    """``mss`` region grab of the composed desktop — the always-available fallback."""

    name = "gdi"
    order = "bgra"  # mss frames are BGRA; the app passes this to the decoder

    def __init__(
        self,
        target: Geometry | dict[str, int] | None = None,
        hwnd: int | None = None,
        *,
        size: tuple[int, int] = SEARCH_REGION,
        refresh_interval: float = GEOMETRY_REFRESH,
        clock: Callable[[], float] = time.monotonic,
        on_geometry: Callable[[Geometry], None] | None = None,
    ):
        if not IS_WINDOWS:
            raise CaptureError("GDI capture is Windows-only")
        import mss  # noqa: PLC0415 - Windows-only dependency

        # mss 10 renamed the class; keep working on 9.x too.
        factory = getattr(mss, "MSS", None) or mss.mss
        self._sct = factory()
        self.size = size
        self._last_ok = 0.0

        geometry: Geometry | None = target if isinstance(target, Geometry) else None
        if geometry is not None:
            self.hwnd = geometry.hwnd
            self.region: dict[str, int] = search_region(geometry.rect, size)
        else:
            self.hwnd = hwnd
            if isinstance(target, dict):
                self.region = {str(key): int(value) for key, value in target.items()}
            else:
                self.region = search_region((0, 0, size[0], size[1]), size)
        self._watcher = _GeometryWatcher(
            geometry, refresh_interval=refresh_interval, clock=clock, on_geometry=on_geometry
        )

    def refresh(self, *, force: bool = False) -> Geometry | None:
        geometry = super().refresh(force=force)
        if geometry is not None:
            self.region = search_region(geometry.rect, self.size)
            self.hwnd = geometry.hwnd
        return geometry

    def grab(self) -> np.ndarray:
        self.refresh()
        try:
            shot = self._sct.grab(self.region)
        except Exception as exc:  # noqa: BLE001 - mss raises ScreenShotError for a dead desktop
            raise CaptureError(f"GDI capture failed: {exc}") from exc
        # mss yields BGRA; the decoder handles the byte order.
        return np.asarray(shot, dtype=np.uint8)[:, :, :4]

    def describe(self) -> str:
        region = self.region
        return f"gdi {region['width']}x{region['height']} at {region['left']},{region['top']}"

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- WGC


class WgcCapture(_WindowBound):
    """``windows-capture`` 2.x (Windows.Graphics.Capture) on the WoW HWND.

    WGC keeps producing frames while another window (our overlay) has focus, which is the
    normal case for this product. The library delivers frames on its own thread; we copy the
    top-left search region out of each one (throttled) so ``grab()`` is a cheap local read.
    Any failure at construction makes :func:`make_capture` fall back to GDI.

    API (windows-capture 2.0.1, ``windows_capture/__init__.py:140-303``)::

        capture = windows_capture.WindowsCapture(window_hwnd=hwnd, cursor_capture=False,
                                                 draw_border=False, minimum_update_interval=100)
        @capture.event
        def on_frame_arrived(frame, capture_control): ...   # frame.frame_buffer is HxWx4 BGRA
        @capture.event
        def on_closed(): ...
        control = capture.start_free_threaded()   # CaptureControl: stop()/wait()/is_finished()
    """

    name = "wgc"
    order = "bgra"  # windows-capture frame_buffer is BGRA

    #: Copy at most ~10 frames/s out of the compositor's stream (a 1600x400 BGRA copy is 2.5 MB).
    FRAME_MIN_INTERVAL = 0.1
    #: Cap the compositor's delivery rate too; WoW redraws at 60+ fps.
    MINIMUM_UPDATE_INTERVAL_MS = 100

    def __init__(
        self,
        geometry: Geometry,
        size: tuple[int, int] = SEARCH_REGION,
        *,
        refresh_interval: float = GEOMETRY_REFRESH,
        clock: Callable[[], float] = time.monotonic,
        on_geometry: Callable[[Geometry], None] | None = None,
        startup_timeout: float = 3.0,
    ):
        if not IS_WINDOWS:
            raise CaptureError("WGC capture is Windows-only")
        import windows_capture  # noqa: PLC0415

        self._module = windows_capture
        self._size = size
        self._frame: np.ndarray | None = None
        self._frame_size: tuple[int, int] | None = None
        self._lock = threading.Lock()
        self._error: str | None = None
        self._closed = False
        self._control = None
        self._last_copy = 0.0
        self._watcher = _GeometryWatcher(
            geometry, refresh_interval=refresh_interval, clock=clock, on_geometry=on_geometry
        )
        self._clock = clock

        self._capture = windows_capture.WindowsCapture(
            window_hwnd=int(geometry.hwnd),
            cursor_capture=False,
            draw_border=False,
            minimum_update_interval=self.MINIMUM_UPDATE_INTERVAL_MS,
        )

        @self._capture.event
        def on_frame_arrived(frame, capture_control):  # noqa: ANN001, ARG001
            self._on_frame(frame)

        @self._capture.event
        def on_closed():
            self._closed = True

        try:
            self._control = self._capture.start_free_threaded()
        except Exception as exc:  # noqa: BLE001 - "Failed to find window: ..." and friends
            raise CaptureError(f"WGC failed to start: {exc}") from exc

        try:
            self._await_first_frame(startup_timeout)
        except CaptureError:
            self.close()
            raise

    def _await_first_frame(self, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self._frame is not None:
                    return
                if self._error:
                    raise CaptureError(f"WGC failed: {self._error}")
            time.sleep(0.02)
        with self._lock:
            if self._error:
                raise CaptureError(f"WGC failed: {self._error}")
        raise CaptureError(f"WGC produced no frame in {timeout:.0f}s")

    # -- callbacks (the library's thread) ----------------------------------
    def _on_frame(self, frame) -> None:  # noqa: ANN001
        """Copy the search region out of a library frame. Must never raise: an exception here
        propagates out of the capture session and kills it."""
        try:
            now = time.monotonic()
            with self._lock:
                if now - self._last_copy < self.FRAME_MIN_INTERVAL:
                    return
                self._last_copy = now
            buffer = np.asarray(frame.frame_buffer)
            if buffer.ndim != 3 or buffer.shape[2] < 4:
                raise ValueError(f"unexpected frame shape {buffer.shape}")
            height, width = buffer.shape[:2]
            crop_w = min(self._size[0], width)
            crop_h = min(self._size[1], height)
            # BGRA, contiguous copy: the buffer is backed by a native mapped frame that is
            # recycled as soon as the callback returns.
            image = np.ascontiguousarray(buffer[:crop_h, :crop_w, :4])
            with self._lock:
                self._frame = image
                self._frame_size = (width, height)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._error = str(exc)

    # -- capture API --------------------------------------------------------
    def client_size(self) -> tuple[int, int] | None:
        """Physical client size: the WGC frame's size once frames flow.

        WGC composes the window in *physical* pixels, while ``GetClientRect`` returns logical
        pixels when the process could not become per-monitor DPI aware (``dpi_state()`` says
        ``already-set``). Taking the frame size keeps the pitch hint correct in that case.
        """
        with self._lock:
            frame_size = self._frame_size
        if frame_size:
            return frame_size
        return super().client_size()

    def grab(self) -> np.ndarray:
        self.refresh()
        with self._lock:
            if self._closed:
                raise NoWowWindow("WGC session closed (the WoW window was closed); re-acquiring")
            if self._error:
                raise CaptureError(f"WGC failed: {self._error}")
            if self._frame is None:
                raise CaptureError("no WGC frame yet")
            return self._frame.copy()

    def describe(self) -> str:
        with self._lock:
            frame_size = self._frame_size
        source = f" of {frame_size[0]}x{frame_size[1]}" if frame_size else ""
        return f"wgc {self._size[0]}x{self._size[1]}{source} (hwnd {self._watcher.geometry.hwnd if self._watcher.geometry else '?'})"

    def close(self) -> None:
        try:
            if self._control is not None:
                self._control.stop()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- factory


_DPI_LOGGED = False

def make_capture(
    *,
    prefer: str = "auto",
    size: tuple[int, int] = SEARCH_REGION,
    logger=None,
) -> Capture:
    """Open a capture source for the current WoW window.

    DPI awareness is set (and logged) before the first window call, the client geometry is
    resolved once, and the returned source re-resolves it every :data:`GEOMETRY_REFRESH`
    seconds on its own.

    Raises :class:`NoWowWindow` when the game is not running so callers can say so instead of
    retrying silently (doc 06 §6.6).
    """
    if not IS_WINDOWS:
        raise NoWowWindow("no WoW window (capture is Windows-only)")
    aware = enable_dpi_awareness()
    global _DPI_LOGGED
    if logger and not _DPI_LOGGED:  # once per process: make_capture is retried every 3 s
        _DPI_LOGGED = True
        suffix = "" if aware else " (client rect may be logical px: WGC frames will not match)"
        logger(f"capture: dpi {dpi_state()}{suffix}")
    window = find_wow_window()
    if window is None:
        raise NoWowWindow("no WoW window")
    geometry = resolve_geometry(window)
    if logger:
        logger(f"capture: window {geometry.describe()} class={geometry.class_name!r}")

    if prefer in ("auto", "wgc"):
        try:
            return WgcCapture(geometry, size)
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger(f"WGC unavailable ({exc}); falling back to GDI")
            if prefer == "wgc":
                raise
    return GdiCapture(geometry, size=size)


class StaticCapture:
    """Test double: returns a fixed image and records how often it was asked."""

    name = "static"

    def __init__(self, image: np.ndarray, order: str = "rgb"):
        self.image = image
        self.order = order
        self.calls = 0

    def grab(self) -> np.ndarray:
        self.calls += 1
        return self.image

    def describe(self) -> str:
        return f"static {self.image.shape[1]}x{self.image.shape[0]}"

    def close(self) -> None:
        pass
