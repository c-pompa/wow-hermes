"""Decode the on-screen pixel strip back into protocol frames (PROTOCOL A.3, doc 06 §6.3/§9.2).

The strip is written by the addon at the top-left of the *client* area: cell ``i`` is a
``4 x 4`` *physical pixel* square at ``((i % 200) * 4, (i // 200) * 4)`` in strip units,
scaled by ``768 / physicalHeight`` and further by the Windows DPI factor when the client is
not per-monitor DPI aware. We therefore never assume a pitch: we try the six plausible
pitches, a small grid of origins, and accept only a frame that passes the preamble check,
the magic/version/length checks *and* the Fletcher-16 checksum, with no ambiguous cell
sampled anywhere.

The decoder is size-agnostic: it decodes whatever crop it is handed and reports
``crop-too-small`` when the frame does not fit. The capture layer decides the crop
(``capture.SEARCH_REGION``: 1600 × 400 physical px, PROTOCOL A.3).

Ambiguity rule (PROTOCOL A.3): a channel that is neither clearly off nor clearly on
(``48 < c < 207``) invalidates the whole frame. The failure mode is always "frame missed",
never "wrong bytes accepted".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np

from . import protocol as p

# DPI hypotheses (doc 06 §6.3): 100 %, 125 %, 150 %, 175 %, 200 % and the 4/3 case.
PITCHES: tuple[float, ...] = (1.0, 1.25, 4 / 3, 1.5, 1.75, 2.0)

#: **Anisotropic** hypotheses (rev 2026-11-09). When WoW renders at a different resolution
#: than the window's client area, the strip is stretched by ``client_w / render_w`` in x and
#: ``client_h / render_h`` in y. Those two factors are usually *not* equal (a 1280x800 render
#: in a 2560x1440 window is 2.0 x 1.8), so the decoder must be able to sample with separate
#: x/y pitches. 1.8 is in the list because 1440/800 is the common 16:9-in-16:10 case.
ANISOTROPIC_SCALES: tuple[float, ...] = (1.0, 1.25, 4 / 3, 1.5, 1.75, 1.8, 2.0)

Pitch = float | tuple[float, float]


def as_pitch(pitch: Pitch) -> tuple[float, float]:
    """Normalise a scalar or ``(x, y)`` pitch hypothesis to a pair of floats."""
    if isinstance(pitch, (int, float)):
        return float(pitch), float(pitch)
    px, py = pitch
    return float(px), float(py)


def label(pitch: tuple[float, float]) -> Pitch:
    """A uniform pair as a plain float (back-compatible), otherwise the pair itself."""
    px, py = pitch
    return px if px == py else pitch

# Preamble: first 9 bits of ``C7 1A`` MSB-first are 110 001 110 -> cells 6, 1, 6.
PREAMBLE = (6, 1, 6)

# Cell index -> how many cells are needed to hold a complete frame of ``length`` payload bytes.
HEADER_CELLS = 19  # ceil(8 * 7 / 3); magic+ver+id+len = 7 bytes


@dataclass(frozen=True)
class DecodedFrame:
    """One accepted frame, plus where it was found (for logging and health reporting)."""

    id: int
    payload: bytes
    pitch: "Pitch"
    origin: tuple[int, int]
    cells: int

    def records(self) -> list[dict]:
        return p.decode_records(self.payload)


@dataclass
class DecodeResult:
    frames: list[DecodedFrame] = field(default_factory=list)
    reason: str = "no-frame"
    pitch: "Pitch | None" = None
    origin: tuple[int, int] | None = None
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.frames)


def to_rgb(img, order: str = "rgb") -> np.ndarray:
    """Normalise an image to a contiguous HxWx3 uint8 RGB array.

    ``order`` is ``rgb``, ``bgr``, ``bgra`` or ``rgba``; 4-channel inputs (mss/WGC) are
    the common case at runtime, 3-channel ones (PIL) in tests.
    """
    arr = np.asarray(img)
    if arr.ndim == 2:
        arr = np.stack((arr, arr, arr), axis=-1)
    if arr.ndim != 3 or arr.shape[2] not in (3, 4):
        raise ValueError(f"expected HxWx3|4 image, got shape {arr.shape}")
    arr = arr.astype(np.uint8, copy=False)
    if arr.shape[2] == 4:
        arr = arr[:, :, :3]
        if order.lower() in ("bgra", "argb"):
            arr = arr[:, :, ::-1]
    elif order.lower() == "bgr":
        arr = arr[:, :, ::-1]
    return np.ascontiguousarray(arr)


def _classify(px) -> int | None:
    """One sampled pixel -> cell value 0..7, or None when any channel is ambiguous."""
    out = 0
    for bit, value in ((4, int(px[0])), (2, int(px[1])), (1, int(px[2]))):
        if value >= 207:
            out |= bit
        elif value > 48:
            return None
    return out


def _cell_origin(ox: int, oy: int, pitch_px: float, pitch_py: float, index: int) -> tuple[int, int]:
    x = ox + int(round((index % p.CELLS_PER_ROW) * p.CELL_PX * pitch_px))
    y = oy + int(round((index // p.CELLS_PER_ROW) * p.CELL_PX * pitch_py))
    return x, y


def _sample(arr: np.ndarray, ox: int, oy: int, pitch: tuple[float, float], index: int) -> int | None:
    """Sample the centre pixel of cell ``index``.

    Returns None for an ambiguous pixel and raises IndexError when the cell centre lies
    outside the crop (callers turn that into a rejection reason).
    """
    h, w = arr.shape[:2]
    pitch_px, pitch_py = pitch
    x, y = _cell_origin(ox, oy, pitch_px, pitch_py, index)
    half_x = int(round(p.CELL_PX * pitch_px / 2.0))
    half_y = int(round(p.CELL_PX * pitch_py / 2.0))
    cx, cy = x + half_x, y + half_y
    if cy >= h or cx >= w or cy < 0 or cx < 0:
        raise IndexError("cell outside crop")
    return _classify(arr[cy, cx])


def _try_decode(arr: np.ndarray, ox: int, oy: int, pitch: tuple[float, float]) -> tuple[DecodedFrame | None, str]:
    """Attempt a full decode for one (origin, pitch) hypothesis."""
    pitch_px, pitch_py = pitch
    try:
        for index, want in enumerate(PREAMBLE):
            if _sample(arr, ox, oy, pitch, index) != want:
                return None, "preamble"
    except IndexError:
        return None, "preamble"

    header: list[int] = []
    for index in range(HEADER_CELLS):
        try:
            value = _sample(arr, ox, oy, pitch, index)
        except IndexError:
            return None, "crop-too-small"
        if value is None:
            return None, "ambiguous"
        header.append(value)

    head = p.cells_to_bytes(header)
    if len(head) < 7:
        return None, "short-header"
    if (head[0], head[1]) != p.MAGIC:
        return None, "magic"
    if head[2] != p.VERSION:
        return None, "version"
    length = (head[5] << 8) | head[6]
    if not 1 <= length <= p.MAX_PAYLOAD:
        return None, "length"

    total = p.cell_count(length)
    if total > p.CELLS_PER_ROW * p.MAX_ROWS:
        return None, "oversize"
    rows = p.row_count(length)
    # The crop must physically contain the last row (the strip is anchored top-left).
    if oy + int(round(rows * p.CELL_PX * pitch_py)) > arr.shape[0]:
        return None, "crop-too-small"

    cells: list[int] = []
    for index in range(total):
        try:
            value = _sample(arr, ox, oy, pitch, index)
        except IndexError:
            return None, "crop-too-small"
        if value is None:
            return None, "ambiguous"
        cells.append(value)

    try:
        frame_id, payload = p.decode_frame(p.cells_to_bytes(cells))
    except ValueError as exc:
        return None, str(exc) or "frame"
    return DecodedFrame(
        id=frame_id, payload=payload, pitch=label(pitch), origin=(ox, oy), cells=total
    ), "ok"


def _anisotropic_pairs() -> tuple[tuple[float, float], ...]:
    """Cross product of :data:`ANISOTROPIC_SCALES`, skipping the uniform pairs.

    Ordered by how far apart the two factors are, so a mild mismatch is found first.
    """
    pairs = [
        (px, py)
        for px in ANISOTROPIC_SCALES
        for py in ANISOTROPIC_SCALES
        if px != py
    ]
    pairs.sort(key=lambda pair: (abs(pair[0] - pair[1]), -max(pair)))
    return tuple(pairs)


ANISOTROPIC_PITCHES: tuple[tuple[float, float], ...] = _anisotropic_pairs()


def decode_image(
    img,
    *,
    order: str = "rgb",
    pitches: tuple[Pitch, ...] = PITCHES,
    pitch_hints: Iterable[Pitch] = (),
    max_offset: int = 6,
    anisotropic: bool = False,
    anisotropic_offset: int = 2,
    max_frames: int = 1,
) -> DecodeResult:
    """Find and decode the strip inside the crop (any size; ``crop-too-small`` if it does not fit).

    ``max_offset`` bounds the origin scan in pixels: the addon anchors the strip at the
    client origin, so anything beyond a couple of pixels is rounding, but we scan a little
    wider to survive half-pixel layout.

    ``pitch_hints`` are tried **first**: the caller can compute them from the client size and
    the hello's ``g=WxH`` (``client_w / g_w``, ``client_h / g_h``), which is the only
    hypothesis that is actually correct when WoW's render resolution differs from the window
    size. ``anisotropic=True`` additionally scans the :data:`ANISOTROPIC_PITCHES` cross
    product (2 px origin scan) when nothing else matched.

    Returns the first accepted frame (only one strip exists) and the rejection reason when
    nothing was accepted.
    """
    arr = to_rgb(img, order)
    if arr.shape[0] < 8 or arr.shape[1] < 40:
        return DecodeResult(reason="crop-too-small")

    attempts = 0
    reasons: list[str] = []

    def scan(hypothesis: tuple[float, float], offset: int) -> DecodeResult | None:
        nonlocal attempts
        for dy in range(0, offset + 1):
            for dx in range(0, offset + 1):
                attempts += 1
                frame, reason = _try_decode(arr, dx, dy, hypothesis)
                if frame is not None:
                    return DecodeResult(
                        frames=[frame], reason="ok", pitch=frame.pitch,
                        origin=(dx, dy), attempts=attempts,
                    )
                reasons.append(reason)
        return None

    seen: set[tuple[float, float]] = set()
    for hint in pitch_hints:
        hypothesis = as_pitch(hint)
        if hypothesis in seen:
            continue
        seen.add(hypothesis)
        found = scan(hypothesis, max_offset)
        if found is not None:
            return found

    for pitch in pitches:
        hypothesis = as_pitch(pitch)
        if hypothesis in seen:
            continue
        seen.add(hypothesis)
        found = scan(hypothesis, max_offset)
        if found is not None:
            return found

    if anisotropic:
        for hypothesis in ANISOTROPIC_PITCHES:
            if hypothesis in seen:
                continue
            seen.add(hypothesis)
            found = scan(hypothesis, anisotropic_offset)
            if found is not None:
                return found

    # Report the most informative failure: a checksum/ambiguity hit on a real candidate is
    # much more useful than the thousands of "preamble" misses.
    for reason in ("ambiguous", "checksum mismatch", "length", "version", "magic"):
        if reason in reasons:
            return DecodeResult(reason=reason, attempts=attempts)
    return DecodeResult(reason="preamble", attempts=attempts)


def decode_search_region(img, *, region: tuple[int, int, int, int] | None = None, **kwargs) -> DecodeResult:
    """Decode a full-frame image, cropping the top-left search region first."""
    arr = np.asarray(img)
    if region is not None:
        x, y, w, h = region
        arr = arr[y : y + h, x : x + w]
    return decode_image(arr, **kwargs)
