"""Inbound side: slot files and `.wav` doorbells (PROTOCOL B).

Everything here is about surviving Windows: a half-written Lua file is a syntax error that
burns a load-on-demand slot for the rest of the UI session, so every write is
``tmp -> fsync -> os.replace`` with the rename retried on WinError 5/32 (a virus scanner or
the client's loader holding the target open). Doorbells are *existence* signals: creating
the file rings, deleting it clears.
"""

from __future__ import annotations

import errno
import os
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import protocol

SLOT_COUNT = 256
TMP_SUFFIX = ".tmp"
SLOT_GLOBAL = "WoWHermes_Slot"

# PROTOCOL B.2: the priority window is the slot the addon says it will load next (the
# ``q`` flag) plus the 15 after it, i.e. the slots it can reach without a doorbell.
WINDOW_SIZE = 16

# ---------------------------------------------------------------- B.2 caps
#
# PROTOCOL B.2 (rev 2026-11-08): every publish writes *all* slots; there is no cursor
# mode. To bound the per-publish cost, a rendered slot file is <= 16 KB: at most four
# replies, each text truncated UTF-8-safely to 3500 bytes with a marker, and a restore
# bundle that takes at most 10 KB (newest first, each entry <= 600 bytes).
MAX_SLOT_BYTES = 16 * 1024
MAX_REPLY_BYTES = 3500
MAX_SLOT_REPLIES = 4
RESTORE_BUDGET = 10 * 1024
RESTORE_ENTRY_BYTES = 600
TRUNCATION_MARKER = " … (full reply in overlay)"


def clip_utf8(text: str, limit: int) -> tuple[str, bool]:
    """Cut ``text`` to at most ``limit`` UTF-8 bytes without splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", "ignore"), True


def truncate_reply(text: str, limit: int = MAX_REPLY_BYTES) -> str:
    """A reply as it appears in a slot: <= limit bytes of text, plus the marker when cut."""
    clipped, cut = clip_utf8(str(text), limit)
    return clipped + TRUNCATION_MARKER if cut else str(text)


def _rendered_size(state: dict, global_name: str) -> int:
    return len(protocol.render_slot(state, global_name).encode("utf-8"))


def _fit_restore(entries: list[dict]) -> list[dict]:
    """Newest entries first, each text <= 600 bytes; the bundle renders to <= 10 KB."""
    ordered = [dict(entry) for entry in reversed(entries)]
    for entry in ordered:
        entry["text"] = clip_utf8(str(entry.get("text") or ""), RESTORE_ENTRY_BYTES)[0]
    while ordered and _rendered_size({"restore": ordered}, SLOT_GLOBAL) > RESTORE_BUDGET:
        ordered.pop()  # drop the oldest entry
    return ordered


def fit_slot_state(
    state: dict,
    *,
    max_bytes: int = MAX_SLOT_BYTES,
    global_name: str = SLOT_GLOBAL,
) -> dict:
    """Apply the B.2 caps and guarantee ``render_slot`` of the result is <= max_bytes.

    Text budgets are measured in UTF-8 *text* bytes, but the rendered file is pure ASCII
    with ``\\ddd`` escapes, so an all-emoji reply costs up to four bytes per source byte.
    The fitting loop therefore measures the rendered size and, while it is too large,
    drops the oldest reply, then shortens the newest one, and only then the restore
    bundle (which itself never exceeds 10 KB). The overlay always keeps the full text.
    """
    out = dict(state)
    replies = []
    for item in (state.get("replies") or [])[-MAX_SLOT_REPLIES:]:
        entry = dict(item)
        entry["text"] = truncate_reply(entry.get("text") or "")
        replies.append(entry)
    out["replies"] = replies
    restore = state.get("restore")
    if restore:
        out["restore"] = _fit_restore(list(restore))
    else:
        out.pop("restore", None)

    def shrink_reply() -> bool:
        if not out["replies"]:
            return False
        entry = out["replies"][0]
        text = str(entry.get("text") or "")
        prefix = text[: -len(TRUNCATION_MARKER)] if text.endswith(TRUNCATION_MARKER) else text
        budget = len(prefix.encode("utf-8")) // 2
        if budget <= 0:
            return False
        entry["text"] = truncate_reply(prefix, budget)
        return True

    while _rendered_size(out, global_name) > max_bytes:
        if len(out["replies"]) > 1:
            out["replies"] = out["replies"][1:]  # oldest first
        elif out["replies"] and out["replies"][0].get("text"):
            if not shrink_reply():
                out["replies"][0]["text"] = ""
        elif out.get("restore"):
            if len(out["restore"]) > 1:
                out["restore"] = out["restore"][1:]
            else:
                out.pop("restore")
        elif out.get("note"):
            out.pop("note")  # the overlay keeps the full text
        elif out.get("reload"):
            out.pop("reload")  # a prompt that no longer fits is useless
        elif out.get("confirm"):
            out.pop("confirm")  # last resort: never send an over-size slot file
        else:
            break
    return out


def silent_wav() -> bytes:
    """The 124-byte silent WAV the doorbell uses: RIFF PCM, 8 kHz, mono, 8-bit, 80 x 0x80."""
    samples = 80
    data = b"\x80" * samples
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(data),
        b"WAVE",
        b"fmt ",
        16,  # fmt chunk size
        1,  # PCM
        1,  # mono
        8000,  # sample rate
        8000,  # byte rate (rate * channels * bits/8)
        1,  # block align
        8,  # bits per sample
        b"data",
        len(data),
    )
    return header + data


SILENT_WAV = silent_wav()


def slot_number(frame_id: int) -> int:
    """Doorbell slot for a frame id: ``((id-1) % 256) + 1`` (PROTOCOL A.4)."""
    return ((int(frame_id) - 1) % SLOT_COUNT) + 1


def doorbell_name(frame_id: int) -> str:
    return f"{slot_number(frame_id):03d}.wav"


def slot_addon_name(index: int) -> str:
    return f"WoWHermes_S{index:03d}"


def window_slots(next_slot: int | None, *, count: int = SLOT_COUNT, size: int = WINDOW_SIZE) -> list[int]:
    """The PROTOCOL B.2 priority window: slots ``q .. q+15``, wrapping inside 1..256.

    ``q = 0`` (pool exhausted) and ``None`` (no ``q`` flag seen yet) both yield an empty
    window — there is no slot the addon is about to load, so nothing is urgent.
    """
    if not next_slot or size <= 0:
        return []
    q = int(next_slot)
    if not 1 <= q <= count:
        return []
    return [((q - 1 + offset) % count) + 1 for offset in range(min(size, count))]



# ---------------------------------------------------------------- atomic write


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if getattr(exc, "winerror", None) in (5, 32):  # ERROR_ACCESS_DENIED / ERROR_SHARING_VIOLATION
        return True
    return getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM, errno.EBUSY)


def atomic_write_bytes(
    path: os.PathLike[str] | str,
    data: bytes,
    *,
    retries: int = 20,
    base_delay: float = 0.004,
    max_delay: float = 0.15,
) -> None:
    """Write ``data`` to ``path`` atomically, retrying the rename while it is locked.

    The temp file lives in the same directory so the rename never crosses volumes. The
    rename is retried because Windows refuses to replace a file another handle has open
    (the game's loader reading the slot, an AV scanner touching the fresh temp file).
    """
    target = Path(path)
    tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex[:8]}{TMP_SUFFIX}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        delay = base_delay
        for attempt in range(retries + 1):
            try:
                os.replace(tmp, target)
                return
            except OSError as exc:
                if not _retryable(exc) or attempt == retries:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, max_delay)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- doorbells


class Doorbells:
    """The two existence pools: ``ack/`` (bridge consumed a frame) and ``sig/`` (reply ready)."""

    KINDS = ("ack", "sig")

    def __init__(self, addon_root: os.PathLike[str] | str):
        self.root = Path(addon_root)
        self.dirs = {kind: self.root / kind for kind in self.KINDS}

    def path(self, kind: str, frame_id: int) -> Path:
        return self.dirs[kind] / doorbell_name(frame_id)

    def exists(self, kind: str, frame_id: int) -> bool:
        return self.path(kind, frame_id).exists()

    def set(self, kind: str, frame_id: int) -> Path:
        path = self.path(kind, frame_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            atomic_write_bytes(path, SILENT_WAV)
        return path

    ring = set

    def clear(self, kind: str, frame_id: int) -> None:
        try:
            self.path(kind, frame_id).unlink()
        except FileNotFoundError:
            pass

    def sweep(self) -> int:
        """Delete every file in both pools — nothing may be pre-raised at startup."""
        removed = 0
        for directory in self.dirs.values():
            directory.mkdir(parents=True, exist_ok=True)
            for entry in directory.iterdir():
                if entry.is_file():
                    try:
                        entry.unlink()
                        removed += 1
                    except OSError:
                        pass
        return removed

    def raised(self, kind: str) -> list[int]:
        directory = self.dirs[kind]
        if not directory.is_dir():
            return []
        return sorted(int(p.stem) for p in directory.glob("*.wav") if p.stem.isdigit())


# ---------------------------------------------------------------- publishing


class SlotsNotInstalled(RuntimeError):
    """The 256 load-on-demand addons are not present / not owned by us."""


@dataclass
class PublishResult:
    seq: int
    mode: str  # "all" (no q yet), "window" (priority window + rest), "background" (q=0)
    slots: int   # owned slot count in the install
    files: int   # files written *synchronously* before publish() returned
    size: int
    window: list[int] = field(default_factory=list)
    targets: list[Path] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.files > 0


class SlotPublisher:
    """Renders ``WoWHermes_Slot`` into the slot addons and ``Inbox.lua`` for /reload.

    Publishing follows PROTOCOL B.2: the *priority window* (the slot the addon will load
    next, ``q``, plus the 15 after it) is written synchronously and ``Inbox.lua`` right
    after it, then a background pass writes the rest of the pool. A newer publish cancels
    the older pass, so at most one pass is in flight and the newest bytes always win.
    Without a ``q`` flag yet (an addon that predates it) the whole pool is written
    synchronously, which is the pre-window behaviour.
    """

    def __init__(
        self,
        addons_dir: os.PathLike[str] | str,
        *,
        count: int = SLOT_COUNT,
        max_bytes: int = MAX_SLOT_BYTES,
        global_name: str = SLOT_GLOBAL,
        window_size: int = WINDOW_SIZE,
    ):
        self.addons_dir = Path(addons_dir)
        self.addon_root = self.addons_dir / "WoWHermes"
        self.count = count
        self.max_bytes = max_bytes
        self.global_name = global_name
        self.window_size = window_size
        self.seq = 0

        # Window writes and background passes are serialised per file write; the
        # generation counter makes a superseded pass stop at the next file.
        self._table_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._generation = 0
        self._cancel = threading.Event()
        self._pass: threading.Thread | None = None
        self._owned_cache: dict[int, Path] | None = None

        # Observability (tests, status): how many files the passes wrote and how many
        # passes a newer publish interrupted.
        self.background_files = 0
        self.completed_passes = 0
        self.cancelled_passes = 0

    # -- discovery ---------------------------------------------------------
    def slot_dir(self, index: int) -> Path:
        return self.addons_dir / slot_addon_name(index)

    def owned_slots(self) -> list[Path]:
        """Only directories carrying our owner marker are ever written to (PROTOCOL §7.4)."""
        owned: list[Path] = []
        for index in range(1, self.count + 1):
            directory = self.slot_dir(index)
            toc = directory / f"{slot_addon_name(index)}.toc"
            try:
                text = toc.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if f"X-WoWHermes-Slot: {index}" in text:
                owned.append(directory)
        return owned

    def _owned_by_index(self) -> dict[int, Path]:
        cached = self._owned_cache
        if cached and all(directory.is_dir() for directory in cached.values()):
            return cached
        owned: dict[int, Path] = {}
        for index in range(1, self.count + 1):
            directory = self.slot_dir(index)
            toc = directory / f"{slot_addon_name(index)}.toc"
            try:
                text = toc.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if f"X-WoWHermes-Slot: {index}" in text:
                owned[index] = directory
        if not owned:
            raise SlotsNotInstalled(
                f"no owned slot addons under {self.addons_dir}; run `wowhermes-bridge install`"
            )
        self._owned_cache = owned
        return owned

    def require_installed(self) -> list[Path]:
        return list(self._owned_by_index().values())

    # -- writing -----------------------------------------------------------
    def render(self, state: dict, seq: int | None = None) -> str:
        payload = dict(state)
        payload["seq"] = self.seq + 1 if seq is None else int(seq)
        fitted = fit_slot_state(payload, max_bytes=self.max_bytes, global_name=self.global_name)
        return protocol.render_slot(fitted, self.global_name)

    def publish(
        self,
        state: dict,
        *,
        next_slot: int | None = None,
        background: bool = True,
        wait: bool = False,
    ) -> PublishResult:
        """Write the priority window now; Inbox.lua and the rest follow in the background.

        ``next_slot`` is the addon's ``q`` flag (1..256, ``0`` = pool exhausted, ``None``
        = no flag seen). With ``None`` the whole pool is written synchronously (the old
        behaviour, kept for addons that do not send ``q`` yet); with ``0`` the window is
        skipped and everything but Inbox.lua is deferred. ``wait=True`` blocks until the
        background pass finishes (benchmarks, tests, shutdown).
        """
        owned = self._owned_by_index()

        with self._table_lock:
            self.seq += 1
            seq = self.seq
            text = self.render(state, seq=seq)
            data = text.encode("utf-8")
            if len(data) > self.max_bytes:  # fit_slot_state is the guarantee; this is the guard
                raise ValueError(f"rendered slot is {len(data)} B, over the {self.max_bytes} B cap")

            # Supersede the pass in flight; it stops at its next file.
            self._cancel.set()
            if self._pass is not None and self._pass.is_alive():
                self.cancelled_passes += 1
            self._generation += 1
            generation = self._generation
            cancel = threading.Event()
            self._cancel = cancel

            window = window_slots(next_slot, count=self.count, size=self.window_size)
            files = 0
            if next_slot is None:
                # No q flag yet: one synchronous full pass (no background needed).
                for index in sorted(owned):
                    self._write(owned[index], data, generation, cancel)
                    files += 1
                self._write_inbox(data)
                files += 1
                return PublishResult(
                    seq=seq, mode="all", slots=len(owned), files=files, size=len(data),
                    window=[], targets=list(owned.values()),
                )

            for index in window:
                directory = owned.get(index)
                if directory is None:
                    continue
                self._write(directory, data, generation, cancel)
                files += 1
            # The /reload fallback is one file and gates the e2e/reload read-back, so it
            # goes out with the window; only the bulk of the pool is deferred.
            self._write_inbox(data)
            files += 1

            remaining = [(index, directory) for index, directory in sorted(owned.items())
                         if index not in window]
            mode = "window" if window else "background"
            if background and remaining:
                self._start_pass(generation, cancel, data, remaining)
            elif remaining:
                for index, directory in remaining:
                    self._write(directory, data, generation, cancel)
                    files += 1

        result = PublishResult(
            seq=seq, mode=mode, slots=len(owned), files=files, size=len(data),
            window=window, targets=[owned[index] for index in window if index in owned],
        )
        if wait:
            self.wait_for_background()
        return result

    # -- background pass ---------------------------------------------------
    def _write(self, directory: Path, data: bytes, generation: int, cancel: threading.Event) -> bool:
        """Write one Slot.lua unless this pass was superseded. Returns True when written."""
        with self._write_lock:
            if cancel.is_set() or generation != self._generation:
                return False
            atomic_write_bytes(directory / "Slot.lua", data)
            return True

    def _write_inbox(self, data: bytes) -> None:
        self.addon_root.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(self.addon_root / "Inbox.lua", data)

    def _start_pass(self, generation: int, cancel: threading.Event, data: bytes,
                    remaining: list[tuple[int, Path]]) -> None:
        def run() -> None:
            written = 0
            try:
                for _index, directory in remaining:
                    if cancel.is_set() or generation != self._generation:
                        return
                    if self._write(directory, data, generation, cancel):
                        written += 1
                    else:
                        return
            finally:
                with self._table_lock:
                    self.background_files += written
                    if not cancel.is_set() and generation == self._generation:
                        self.completed_passes += 1

        thread = threading.Thread(target=run, name="wowhermes-slotpass", daemon=True)
        self._pass = thread
        thread.start()

    def wait_for_background(self, timeout: float | None = 30.0) -> bool:
        """Block until the latest background pass ends. Returns False on timeout."""
        thread = self._pass
        if thread is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True

    @property
    def background_pending(self) -> bool:
        thread = self._pass
        return thread is not None and thread.is_alive()
