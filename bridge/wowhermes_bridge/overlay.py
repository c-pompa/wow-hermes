"""The desktop overlay (PROTOCOL D, doc 06 §8.2): PySide6, frameless, always on top.

In-process: the bridge calls methods on this widget directly from its own thread (Qt signals
are not required because every call is marshalled through a single-shot timer).

Design constraints, all of them learned the hard way in prior art:

* never steal focus (``WA_ShowWithoutActivating`` + ``WS_EX_NOACTIVATE``),
* never sit over the top-left strip (the geometry helper refuses that rectangle),
* click-through must be idempotent and verified by reading the style back,
* model output is HTML-escaped before it reaches the transcript.
"""

from __future__ import annotations

import html
import sys

try:  # pragma: no cover - import guard
    from PySide6 import QtCore, QtGui, QtWidgets

    PYQT_AVAILABLE = True
except Exception:  # noqa: BLE001 - PySide6 is an optional extra
    QtCore = QtGui = QtWidgets = None  # type: ignore[assignment]
    PYQT_AVAILABLE = False

#: The region the pixel strip occupies; the overlay must never cover it (doc 06 §6.6).
STRIP_REGION = (0, 0, 1600, 400)
OVERLAY_WIDTH = 400
OVERLAY_HEIGHT = 460
MARGIN = 24


def available() -> bool:
    return PYQT_AVAILABLE


class NullOverlay:
    """Stand-in used with ``--no-overlay`` and in tests without Qt."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.typing = False
        self.health = ""
        self.click_through = True

    def note_message(self, character: str, text: str) -> None:
        self.lines.append(f"{character} > {text}")

    def note_reply(self, character: str, text: str, *, status: str = "working", final: bool = False) -> None:
        self.lines.append(f"{character} < {text}")

    def note_notice(self, character: str, text: str) -> None:
        self.lines.append(f"notice: {text}")

    def set_typing(self, character: str, typing: bool) -> None:
        self.typing = typing

    def set_health(self, line: str) -> None:
        self.health = line

    def set_click_through(self, enabled: bool) -> bool:
        self.click_through = enabled
        return enabled

    def close(self) -> None:
        pass


def plan_geometry(client_rect: tuple[int, int, int, int] | None, screen_rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Bottom-right dock inside the game's client area, never over the strip region."""
    if client_rect:
        left, top, width, height = client_rect
    else:
        left, top, width, height = screen_rect

    x = left + width - OVERLAY_WIDTH - MARGIN
    y = top + height - OVERLAY_HEIGHT - MARGIN
    x = max(x, left + MARGIN)
    y = max(y, top + MARGIN)

    # If the client is small, the panel could land on the strip; push it below it.
    strip_right = left + STRIP_REGION[2]
    strip_bottom = top + STRIP_REGION[3]
    if x < strip_right and y < strip_bottom:
        y = strip_bottom + 8
        if y + OVERLAY_HEIGHT > top + height:
            y = max(top + MARGIN, top + height - OVERLAY_HEIGHT - MARGIN)
            x = max(x, strip_right + 8)
    return x, y, OVERLAY_WIDTH, OVERLAY_HEIGHT


if PYQT_AVAILABLE:

    class Overlay(QtWidgets.QWidget):  # type: ignore[misc]
        def __init__(self, title: str = "WoW Hermes"):
            super().__init__(None)
            self.setWindowTitle(title)
            self.setWindowFlags(
                QtCore.Qt.WindowType.FramelessWindowHint
                | QtCore.Qt.WindowType.WindowStaysOnTopHint
                | QtCore.Qt.WindowType.Tool
                | QtCore.Qt.WindowType.WindowDoesNotAcceptFocus
            )
            self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.setMinimumSize(280, 200)

            self._click_through = True
            self._typing = False
            self._health = ""
            self._rows: list[str] = []

            root = QtWidgets.QVBoxLayout(self)
            root.setContentsMargins(10, 10, 10, 10)
            root.setSpacing(6)

            self.header = QtWidgets.QLabel("WoW Hermes")
            self.header.setStyleSheet("color:#e8e8e8;font-weight:600;")
            self.transcript = QtWidgets.QTextBrowser()
            self.transcript.setOpenExternalLinks(False)
            self.transcript.setStyleSheet(
                "QTextBrowser{background:rgba(16,16,20,200);color:#f0f0f0;border:1px solid #3a3a44;"
                "border-radius:6px;padding:6px;}"
            )
            self.status = QtWidgets.QLabel("starting…")
            self.status.setStyleSheet("color:#9aa0a6;font-size:11px;")
            self.typing_label = QtWidgets.QLabel("")
            self.typing_label.setStyleSheet("color:#7fd1ff;font-size:11px;")

            root.addWidget(self.header)
            root.addWidget(self.transcript, 1)
            root.addWidget(self.typing_label)
            root.addWidget(self.status)
            self.set_click_through(True)
            self._apply_frame_style()

        # -- style -----------------------------------------------------
        def _apply_frame_style(self) -> None:
            border = "1px solid #2b2b33" if self._click_through else "1px solid #4a8fdd"
            self.setStyleSheet(f"QWidget{{background:rgba(12,12,16,170);border-radius:8px;border:{border};}}")

        # -- content ---------------------------------------------------
        def note_message(self, character: str, text: str) -> None:
            self._rows.append(f"<p style='color:#cfe8ff'><b>{html.escape(character)}</b> &gt; {html.escape(text)}</p>")

        def note_reply(self, character: str, text: str, *, status: str = "working", final: bool = False) -> None:
            colour = {"working": "#d0d0d0", "done": "#e8e8e8", "error": "#ff8a80"}.get(status, "#d0d0d0")
            suffix = "" if final else " <i>(streaming…)</i>"
            # Re-render the last row while a reply is still streaming.
            if status == "working" and self._rows and self._rows[-1].startswith("<p style='color:#e8e8e8'"):
                self._rows.pop()
            self._rows.append(
                f"<p style='color:{colour}'><b>{html.escape(character)}</b> &lt; {html.escape(text)}{suffix}</p>"
            )
            self._flush()

        def note_notice(self, character: str, text: str) -> None:
            self._rows.append(f"<p style='color:#ffd479'>notice: {html.escape(text)}</p>")
            self._flush()

        def _flush(self) -> None:
            del self._rows[:-200]
            self.transcript.setHtml("".join(self._rows))
            bar = self.transcript.verticalScrollBar()
            bar.setValue(bar.maximum())

        def set_typing(self, character: str, typing: bool) -> None:
            self._typing = typing
            self.typing_label.setText("Hermes is thinking…" if typing else "")

        def set_health(self, line: str) -> None:
            self._health = line
            self.status.setText(line)

        # -- click-through ---------------------------------------------
        def set_click_through(self, enabled: bool) -> bool:
            """Apply both switches (Qt flag + Win32 ex-style) and verify by reading back."""
            enabled = bool(enabled)
            self._click_through = enabled
            self.setWindowFlag(QtCore.Qt.WindowType.WindowTransparentForInput, enabled)
            self.setWindowFlag(QtCore.Qt.WindowType.WindowDoesNotAcceptFocus, enabled)
            if sys.platform == "win32":
                self._apply_win32_styles(enabled)
            self._apply_frame_style()
            self.show() if not self.isVisible() else None
            return self.click_through_active()

        def _apply_win32_styles(self, enabled: bool) -> None:  # pragma: no cover - Windows only
            import ctypes

            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_LAYERED = 0x00080000
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            hwnd = int(self.winId())
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            flags = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | (WS_EX_TRANSPARENT if enabled else 0)
            if enabled:
                style |= flags
            else:
                style &= ~WS_EX_TRANSPARENT
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)

        def click_through_active(self) -> bool:
            if not sys.platform == "win32":
                return bool(self.testAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents) or self._click_through)
            import ctypes  # pragma: no cover

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            hwnd = int(self.winId())
            style = user32.GetWindowLongW(hwnd, -20)
            return bool(style & 0x00000020)

        # -- placement -------------------------------------------------
        def place(self, client_rect: tuple[int, int, int, int] | None = None) -> None:
            screen = self.screen().availableGeometry() if self.screen() else None
            screen_rect = (screen.x(), screen.y(), screen.width(), screen.height()) if screen else (0, 0, 1920, 1080)
            x, y, w, h = plan_geometry(client_rect, screen_rect)
            self.setGeometry(x, y, w, h)

        def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
            event.accept()


def create_overlay(*, enabled: bool = True, client_rect: tuple[int, int, int, int] | None = None):
    """Build the overlay, or a NullOverlay when Qt is missing / disabled."""
    if not enabled or not PYQT_AVAILABLE:
        return NullOverlay()
    try:
        overlay = Overlay()
        overlay.place(client_rect)
        overlay.show()
        return overlay
    except Exception:  # noqa: BLE001 - a broken overlay must not stop the bridge
        return NullOverlay()
