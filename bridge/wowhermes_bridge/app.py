"""The bridge loop (doc 06 §9.8, PROTOCOL A.4 / B / C).

Order matters and is the contract:

    capture -> decode -> dedupe on (session, id) -> ack doorbell -> publish "working"
            -> POST /v1/wow/message -> ... outbox reply -> publish + sig doorbell

Every step is a method so tests can drive them one at a time with a fake capture source and
a fake gateway; ``run()`` is the thin scheduling shell.
"""

from __future__ import annotations

import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, Protocol

import numpy as np

from . import commands, decoder
from . import addons as addons_pkg
from .capture import CaptureError, NoWowWindow
from .config import Config
from .confirms import ConfirmStore
from .hermes_client import (
    ConfigError,
    HermesClient,
    HermesClientError,
    OutboxBatch,
    OutboxPoller,
    resolve_outbox_cursor,
)
from .net import Fetcher

from .savedvars import LuaParseError, outbox_entries, parse_file
from .slots import Doorbells, SlotPublisher, SlotsNotInstalled
from .state import BridgeState, Exchange, build_slot_state

IDLE_INTERVAL = 1.0
BUSY_INTERVAL = 0.25
PUBLISH_MIN_INTERVAL = 0.5
SIG_GRACE_PUBLISHES = 3

# PROTOCOL B.2 slot key ``reload``: after a successful addon.install the addon offers a
# 10 s UI reload countdown. The bridge republishes the prompt for this long so a slow
# slot load / a /reload in the middle still sees it; the addon dedupes on ``ts``.
RELOAD_PROMPT_SECONDS = 60.0

#: The hello's ``g=WxH`` render size (PROTOCOL A.1).
_GEOM_RE = re.compile(r"^(\d{2,5})\s*x\s*(\d{2,5})$")

# PROTOCOL B.3: an ack doorbell lives at least 2 s *and* one publish generation (the
# addon polls ack/ every 0.25 s, so a publish must never delete it before the game sees
# it), and at most 60 s even if no publish happens.
ACK_MIN_LIFETIME = 2.0
ACK_MAX_LIFETIME = 60.0


class CaptureSource(Protocol):
    def grab(self) -> np.ndarray: ...


class Gateway(Protocol):
    def post_message(self, **kwargs) -> dict: ...

    def get_outbox(self, character: str, since: int = 0, wait: float = 0): ...


__all__ = ["BridgeApp", "CaptureError", "NoWowWindow", "TickReport"]


@dataclass
class TickReport:
    captured: bool = False
    decode_reason: str | None = None
    frames: int = 0
    records: int = 0
    messages: int = 0
    outbox_frames: int = 0
    published: bool = False
    error: str | None = None


@dataclass
class BridgeHealth:
    capture: str = "unknown"  # ok | no-window | error | unknown
    gateway: str = "unknown"  # ok | error | unauthorized | unknown
    last_frame: float = 0.0
    last_error: str = ""
    capture_detail: str = ""

    def line(self) -> str:
        moment = time.strftime("%H:%M:%S", time.localtime(self.last_frame)) if self.last_frame else "never"
        detail = f" ({self.capture_detail})" if self.capture_detail else ""
        text = f"capture {self.capture}{detail} · gateway {self.gateway} · last frame {moment}"
        if self.gateway == "config-error" and self.last_error:
            text += f" · {self.last_error}"
        return text


class BridgeApp:
    def __init__(
        self,
        config: Config,
        *,
        capture: CaptureSource | None = None,
        gateway: Gateway | None = None,
        state: BridgeState | None = None,
        publisher: SlotPublisher | None = None,
        bells: Doorbells | None = None,
        overlay=None,
        client: HermesClient | None = None,
        logger: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.time,
        capture_factory: Callable[[], CaptureSource] | None = None,
        confirms: ConfirmStore | None = None,
        fetcher: Fetcher | None = None,
        game_running: Callable[[], bool] | None = None,
        opener: Callable[[str], object] | None = None,
    ):
        self.config = config
        self.capture = capture
        # Re-acquire the WoW window when the bridge started before the game (in-game round 1:
        # the bridge said "no WoW window" once and never looked again).
        self.capture_factory = capture_factory
        self._capture_retry_at = 0.0
        # A HermesClient is both the POST/long-poll gateway and the outbox poller. In-game round 5
        # the CLI passed only client=, so every message failed with "gateway not configured".
        self.gateway = gateway if gateway is not None else client
        self.client = client
        self.state = state or BridgeState(config.transcripts_path)
        self.publisher = publisher or SlotPublisher(config.addons_dir, count=config.slot_count)
        self.bells = bells or Doorbells(config.addon_root)
        self.overlay = overlay
        self.health = BridgeHealth()
        self.character = config.character or ""
        self.clock = clock
        self.boot = self.clock()
        self._log = logger or (lambda message: None)
        self._lock = threading.RLock()

        self._acks_raised: dict[int, tuple[float, int]] = {}  # frame id -> (raised at, publish generation)
        self._sig_ids: set[int] = set()
        self._pending_sigs: dict[int, Exchange] = {}  # rung only after the publish lands
        self._generation = 0
        self._last_publish = 0.0
        self._window: list[int] = []
        self._restore: list[dict] | None = None
        self._restore_publishes = 0
        self._context: dict = {}
        self._savedvars_mtime: float | None = None
        self._pollers: dict[str, OutboxPoller] = {}
        self._stop = threading.Event()
        self._pending_clock: float = 0.0
        self._config_error: str | None = None
        self._last_pitch: object = None

        # v1.2 confirmed commands (WS-H): pending Accepts, the download client and the
        # local notices we also push in-game (slot key ``note``).
        self.confirms = confirms or ConfirmStore(clock=clock)
        self.fetcher = fetcher or Fetcher()
        self._game_running = game_running or addons_pkg.game_running
        self._opener = opener
        self._notices: dict[str, str] = {}
        # character -> {"name", "ts", "exp"}: fresh post-install reload prompts (B.2 `reload`)
        self._reloads: dict[str, dict] = {}

    # ---------------------------------------------------------------- setup
    def start(self) -> None:
        self.state.load()
        removed = self.bells.sweep()
        if removed:
            self._log(f"doorbell sweep removed {removed} stale file(s)")
        if self.config.character and not self.character:
            self.character = self.config.character
        try:
            self.publish(bridge="starting", force=True)
        except SlotsNotInstalled as exc:
            self.health.last_error = str(exc)
            self._log(f"warning: {exc}")

    # ---------------------------------------------------------------- capture
    def _try_acquire_capture(self) -> None:
        if self.capture_factory is None or self.clock() < self._capture_retry_at:
            return
        self._capture_retry_at = self.clock() + 3.0
        try:
            self.capture = self.capture_factory()
        except Exception as exc:  # noqa: BLE001 - NoWowWindow / CaptureError: keep retrying
            self.health.capture = "no-window"
            self.health.capture_detail = str(exc)
            return
        if hasattr(type(self.capture), "on_geometry") or hasattr(self.capture, "on_geometry"):
            setattr(self.capture, "on_geometry", self._on_geometry)
        self._log(f"capture: acquired {self.capture.describe()}")
        self._place_overlay()

    def _release_capture(self) -> None:
        """Drop a capture source so the next tick re-acquires it (window closed / moved off)."""
        source, self.capture = self.capture, None
        self._capture_retry_at = self.clock() + 1.0
        if source is None:
            return
        close = getattr(source, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass

    def _on_geometry(self, geometry) -> None:
        """The WoW client area moved or resized: log it and re-place our overlay."""
        self._log(f"capture: window {geometry.describe()}")
        self._place_overlay(geometry.rect)

    def _place_overlay(self, rect: tuple[int, int, int, int] | None = None) -> None:
        place = getattr(self.overlay, "place", None)
        if not callable(place):
            return
        try:
            from .capture import client_rect, find_wow_window

            window = find_wow_window()
            place(rect or (client_rect(window.hwnd) if window else None))
        except Exception as exc:  # noqa: BLE001
            self._log(f"overlay: re-place failed ({exc})")

    def _pitch_hints(self) -> list[tuple[float, float]]:
        """Anisotropic pitch hypotheses from the hello's render size and the client size.

        WoW scales its render target to the window, so when the two differ the strip is
        stretched by ``client_w / g_w`` horizontally and ``client_h / g_h`` vertically —
        usually different factors (2.0 x 1.8 for a 1280x800 render in a 2560x1440 window).
        Only a hello that was already decoded can tell us ``g``.
        """
        if not self.character or self.capture is None:
            return []
        match = _GEOM_RE.match(str(self.state.hello(self.character).get("g") or "").strip())
        size_of = getattr(self.capture, "client_size", None)
        client = size_of() if callable(size_of) else None
        if match is None or not client:
            return []
        gw, gh = int(match.group(1)), int(match.group(2))
        cw, ch = client
        if not (gw and gh and cw and ch):
            return []
        px, py = cw / gw, ch / gh
        if not (0.5 <= px <= 4.0 and 0.5 <= py <= 4.0):
            return []
        return [(round(px, 3), round(py, 3))]

    def tick(self) -> TickReport:
        report = TickReport()
        if self.capture is None:
            self._try_acquire_capture()
        if self.capture is None:
            report.decode_reason = "no-capture"
            self._expire_confirms()
            self._tick_gateway(report)
            return report
        try:
            image = self.capture.grab()
        except NoWowWindow as exc:
            self.health.capture = "no-window"
            self.health.capture_detail = str(exc)
            report.error = str(exc)
            if self.capture_factory is not None:
                self._release_capture()
            self._expire_confirms()
            self._tick_gateway(report)
            return report
        except CaptureError as exc:
            self.health.capture = "error"
            self.health.capture_detail = str(exc)
            report.error = str(exc)
            self._expire_confirms()
            self._tick_gateway(report)
            return report

        report.captured = True
        hints = self._pitch_hints()
        # Live round 3: WGC/GDI hand over BGRA, and decoding it as RGB swapped R/B, so every frame
        # failed the magic check ("preamble"). The capture source declares its byte order.
        order = getattr(self.capture, "order", "rgb")
        result = decoder.decode_image(image, order=order, pitch_hints=hints, anisotropic=True)
        report.decode_reason = result.reason
        if result.ok:
            self.health.capture = "ok"
            self.health.last_frame = self.clock()
            self._note_pitch(result.pitch)
            with self._lock:
                for frame in result.frames:
                    report.records += self.handle_frame(frame)
                report.frames = len(result.frames)
        else:
            self.health.capture = "ok" if self.health.capture == "ok" else "no-frame"
            self._diagnose_no_frame(image, result.reason, hints=hints)

        self._clear_old_acks()
        self._expire_confirms()
        self._tick_gateway(report)
        return report

    def _note_pitch(self, pitch) -> None:
        """Log an anisotropic decode once per (x, y) so the log shows the real scale factors."""
        if not isinstance(pitch, tuple) or pitch == self._last_pitch:
            return
        self._last_pitch = pitch
        self._log(f"decode: ok at anisotropic pitch {pitch[0]:g}x{pitch[1]:g} (client vs hello g)")

    def _diagnose_no_frame(self, image, reason: str, *, hints: list[tuple[float, float]] | None = None) -> None:
        """Every 15 s without a frame: log the decoder's reason and keep the last capture on disk
        (%LOCALAPPDATA%\\wowhermes\\last_capture.png) so a live failure can be inspected remotely."""
        now = self.clock()
        if now < getattr(self, "_diag_next", 0.0):
            return
        self._diag_next = now + 15.0
        describe = getattr(self.capture, "describe", None)
        detail = describe() if callable(describe) else "no capture"
        hint_text = f"; hints {hints}" if hints else ""
        self._log(
            f"decode: no frame ({reason}); capture {detail}; image {getattr(image, 'shape', None)}{hint_text}"
        )
        try:
            from PIL import Image
            import numpy as np

            from .config import config_dir

            arr = np.asarray(image)
            if arr.ndim == 3 and arr.shape[2] >= 3:
                rgb = arr[:, :, [2, 1, 0]] if arr.shape[2] == 4 else arr  # capture is BGRA
                out = config_dir() / "last_capture.png"
                out.parent.mkdir(parents=True, exist_ok=True)
                Image.fromarray(rgb.astype("uint8")).save(out)
        except Exception as exc:  # noqa: BLE001 - diagnostics must never break the loop
            self._log(f"decode: could not save last_capture.png ({exc})")

    def _note_config_error(self, exc: BaseException) -> None:
        """A 401 is a configuration error: say it once, then stop calling the gateway."""
        message = (
            "gateway rejected the bearer token (401); fix WOWHERMES_TOKEN / token in config.toml "
            "and restart the bridge"
        )
        self.health.gateway = "config-error"
        self.health.last_error = message
        if self._config_error is None:
            self._config_error = message
            self._log(f"CONFIGURATION ERROR: {message} ({exc})")
        if self.overlay is not None and hasattr(self.overlay, "set_health"):
            self.overlay.set_health(self.health.line())

    def _tick_gateway(self, report: TickReport) -> None:
        character = self.character
        if not character or self._config_error:
            return
        try:
            processed = self.poll_outbox(character, wait=0)
        except ConfigError as exc:
            report.error = str(exc)
            self._note_config_error(exc)
            return
        except Exception as exc:  # noqa: BLE001 - a gateway hiccup must not kill the loop
            report.error = str(exc)
            self.health.gateway = "error"
            return
        report.outbox_frames = processed

    def next_interval(self) -> float:
        busy = any(e.status == "working" for e in self.state.exchanges.values())
        return BUSY_INTERVAL if busy else IDLE_INTERVAL

    # ---------------------------------------------------------------- inbound
    def handle_frame(self, frame: decoder.DecodedFrame) -> int:
        """One decoded strip frame -> handled records. Returns the record count.

        Dedupe is per **frame**, not per record (PROTOCOL A.1): every record of a frame
        is processed, then ``(session, id)`` is marked handled. A context ``k`` record
        precedes an ``m`` in the same frame and must never consume the id, or the message
        is lost.
        """
        try:
            records = frame.records()
        except (ValueError, UnicodeDecodeError) as exc:
            self._log(f"bad payload on frame {frame.id}: {exc}")
            return 0
        if not records:
            return 0
        first = records[0]
        session = str(first.get("session") or "")
        character = self._character_for(first)
        if self.state.is_handled(character, session, frame.id):
            return 0
        count = 0
        for record in records:
            try:
                self.handle_record(record, from_frame=True)
            except Exception as exc:  # noqa: BLE001
                self._log(f"record {record.get('kind')!r} failed: {exc}")
                self.health.last_error = str(exc)
            count += 1
        # Mark once, after the whole frame; also under the character a hello may adopt,
        # so a re-capture of the same hello frame is ignored next tick.
        self.state.mark_handled(character, session, frame.id)
        if self.character and self.character != character:
            self.state.mark_handled(self.character, session, frame.id)
        # Ack EVERY decoded frame (hello/bye/error too), not only messages. In-game round 4 the
        # hello strip stayed on screen: hellos were never acked, so the addon redrew them 3x20 s
        # and re-sent a hello every 60 s, which kept the strip up almost permanently.
        acked_as = self.character or character
        if self.state.ack(acked_as) < frame.id:
            self.state.set_ack(acked_as, frame.id)
            if self._sound_available(acked_as):
                self.bells.set("ack", frame.id)
            self._acks_raised.setdefault(frame.id, (self.clock(), self._generation))
            self.publish(bridge="up", force=True)
        return count

    def handle_record(self, record: dict, *, from_frame: bool = False) -> None:
        """One record. Standalone records (reload mode) are deduped on ``(session, id)``.

        ``from_frame=True`` is used by :meth:`handle_frame`, which owns the dedupe; a
        ``k`` record never marks an id in either mode.
        """
        kind = record.get("kind")
        character = self._character_for(record)
        session = str(record.get("session") or "")
        frame_id = int(record.get("id") or 0)
        text = str(record.get("text") or "")

        if not from_frame and kind in ("h", "m", "x", "e", "c"):
            if self.state.is_handled(character, session, frame_id):
                return

        if kind == "h":
            self._handle_hello(character, session, frame_id, text, record)
        elif kind == "k":
            from .protocol import parse_kv

            context = parse_kv(text)
            self.state.set_context(character, context)
            self._context = context
            self._note_flags(character, session, record)
            # A `k` never consumes the id: an `m` with the same id follows in the frame.
        elif kind == "m":
            self._handle_message(character, session, frame_id, str(record.get("chat") or "c0"), text, record)
        elif kind == "q":
            # PROTOCOL A.1 v1.1 quest snapshot: it rides the m's frame id, never marks it.
            self.state.set_quest(character, text)
            self._log(f"{character}: quest snapshot {text[:120]}")
        elif kind == "x":
            self._log(f"{character}: bye (PLAYER_LOGOUT)")
            self.state.mark_handled(character, session, frame_id)
            self.publish(bridge="down", force=True)
        elif kind == "e":
            self._log(f"{character}: addon error: {text[:300]}")
            self.state.mark_handled(character, session, frame_id)
        elif kind == "c":
            # PROTOCOL A.1 `c`: the player pressed Accept/Cancel on a command's popup.
            self._handle_confirm(character, session, text)
            if not from_frame:
                self.state.mark_handled(character, session, frame_id)
        else:
            self._log(f"{character}: unknown record kind {kind!r}")

    def _note_flags(self, character: str, session: str, record: dict) -> None:
        """Track the ``q`` flag (next slot) from h/m records for the priority window."""
        from .protocol import parse_kv

        flags = parse_kv(str(record.get("flags") or ""))
        if "q" not in flags:
            return
        try:
            q = int(flags["q"])
        except (TypeError, ValueError):
            return
        if 0 <= q <= 256:
            self.state.note_next_slot(character, session, q)

    def _character_for(self, record: dict) -> str:
        known = self.character
        if not known:
            known = str(self.config.character or "")
        if known:
            return known
        # No hello yet: derive a placeholder so nothing is dropped, then adopt the hello.
        session = str(record.get("session") or "unknown")
        return f"unknown-{session}"

    def _handle_hello(self, character: str, session: str, frame_id: int, text: str, record: dict | None = None) -> None:
        from .protocol import parse_kv

        fields = parse_kv(text)
        announced = fields.get("char") or ""
        if announced and announced != self.character:
            if self.character and self.character.startswith("unknown-"):
                # adopt the real name: move nothing, just switch from now on
                pass
            self.character = announced
            character = announced
        new_session = self.state.set_session(character, session)
        if new_session:
            self.state.reset_next_slot(character)
            # ids restart with a new session token: the old high-water mark must not
            # pre-ack the new session's frames
            self.state.set_ack(character, 0, force=True)
        if record is not None:
            self._note_flags(character, session, record)
        self.state.note_hello(character, fields)
        last = fields.get("n")
        if last and str(last).isdigit():
            self.state.set_ack(character, int(last))
            self._clear_sigs_up_to(int(last))
        if new_session:
            bundle = self.state.restore_bundle(character)
            self._restore = bundle
            self._restore_publishes = SIG_GRACE_PUBLISHES if bundle else 0
            self._log(f"{character}: new session {session}, restore={'yes' if bundle else 'no'}")
        self._log(
            f"{character}: hello v={fields.get('v')} build={fields.get('b')} iface={fields.get('i')} "
            f"slots={fields.get('p')} sound={fields.get('s')} geom={fields.get('g')} scale={fields.get('u')}"
        )
        self.publish(bridge="up", force=True)

    def _handle_message(self, character: str, session: str, frame_id: int, chat: str, text: str,
                        record: dict | None = None) -> None:
        if record is not None:
            self._note_flags(character, session, record)
        self.state.mark_handled(character, session, frame_id)
        now = self.clock()
        self.state.add_message(character, "user", id=frame_id, text=text, t=now)
        self.state.set_ack(character, frame_id)
        if self._sound_available(character):
            self.bells.set("ack", frame_id)
        self._acks_raised[frame_id] = (now, self._generation)

        exchange = Exchange(
            character=character,
            chat=chat or "c0",
            id=frame_id,
            session=session,
            text=text,
            created=now,
            updated=now,
        )
        self.state.put_exchange(exchange)
        self.publish(bridge="up", force=True)
        if self.overlay is not None and hasattr(self.overlay, "note_message"):
            self.overlay.note_message(character, text)

        if self.gateway is None or self._config_error:
            exchange.status = "error"
            exchange.error = self._config_error or "gateway not configured"
            self.state.add_message(character, "hermes", id=exchange.id, text=exchange.error, t=now, status="error")
            self.publish(bridge="down", force=True)
            return

        try:
            context = self.state.context(character)
            quest = self.state.quest(character)
            if quest:
                # Additive key inside the existing PROTOCOL C context dict; the plugin can
                # use it for quest help and older readers ignore unknown keys.
                context = {**context, "quest": quest}
            response = self.gateway.post_message(
                character=character,
                chat=exchange.chat,
                msg_id=exchange.key,
                text=text,
                ts=now,
                context=context,
                client={
                    "build": self.state.hello(character).get("b", ""),
                    "interface": int(self.state.hello(character).get("i") or 0),
                },
            )
        except ConfigError as exc:
            exchange.status = "error"
            exchange.error = "bridge token rejected (401); fix WOWHERMES_TOKEN or config.toml"
            self.state.add_message(character, "hermes", id=exchange.id, text=exchange.error, t=now, status="error")
            self._note_config_error(exc)
            self.publish(bridge="down", force=True)
            return
        except HermesClientError as exc:
            exchange.status = "error"
            exchange.error = f"gateway error: {exc}"
            self.health.gateway = "error"
            self._log(f"POST failed: {exc}")
            self.publish(bridge="down", force=True)
            return

        self.health.gateway = "ok"
        if isinstance(response, dict) and response.get("accepted") is False:
            reason = str(response.get("reason") or "rejected")
            explanation = {
                "unauthorized": "character not allowed: add it to WOW_ALLOWED_USERS (or set WOW_ALLOW_ALL_USERS=true)",
                "duplicate": "duplicate message: this one was already delivered",
            }.get(reason, f"gateway rejected the message ({reason})")
            exchange.status = "error"
            exchange.error = explanation
            exchange.updated = now
            self.state.add_message(
                character, "hermes", id=exchange.id, text=explanation, t=now, status="error"
            )
            self._log(f"{character}: {exchange.key} rejected: {reason}")
            self.publish(force=True)
        self._log(f"{character}: sent {exchange.key} ({len(text)} chars)")

    def _sound_available(self, character: str) -> bool:
        """Doorbells only make sense in mode A (existence signalling)."""
        return str(self.state.bucket(character).get("sound_mode") or "0").upper() == "A"

    # ---------------------------------------------------------------- outbox
    def handle_outbox_frames(self, character: str, frames: list[dict]) -> int:
        processed = 0
        with self._lock:
            for frame in frames:
                try:
                    self._handle_outbox_frame(character, frame)
                except Exception as exc:  # noqa: BLE001
                    self._log(f"outbox frame failed: {exc}")
                processed += 1
            if processed:
                published = self.publish(force=True)
                if published:
                    self._release_sigs()
        return processed

    def _handle_outbox_frame(self, character: str, frame: dict) -> None:
        kind = str(frame.get("kind") or "")
        reply_to = frame.get("reply_to")
        now = self.clock()

        if kind == "typing":
            if self.overlay is not None and hasattr(self.overlay, "set_typing"):
                self.overlay.set_typing(character, True)
            return

        if kind == "command":
            self._handle_command(character, frame)
            return

        exchange = self.state.get_exchange(str(reply_to)) if reply_to else None
        text = str(frame.get("text") or "")

        if kind == "notice":
            self._log(f"gateway notice: {text}")
            if self.overlay is not None and hasattr(self.overlay, "note_notice"):
                self.overlay.note_notice(character, text)
            return

        if exchange is None:
            if not text:
                return
            exchange = self.state.new_synthetic(character, text, t=now)
            self.state.add_message(character, "hermes", id=exchange.id, text=text, t=now)
            if self.overlay is not None and hasattr(self.overlay, "note_reply"):
                self.overlay.note_reply(character, text, status="done", final=True)
            return

        if kind in ("reply", "done"):
            if text:
                exchange.reply = (exchange.reply + "\n" + text) if exchange.reply else text
                exchange.updated = now
                self.state.add_message(
                    character, "hermes", id=exchange.id, text=exchange.reply, t=now, status=exchange.status
                )
                if self.overlay is not None and hasattr(self.overlay, "note_reply"):
                    self.overlay.note_reply(character, exchange.reply, status="working", final=kind == "done")

            finished = kind == "done"
            if finished:
                exchange.status = "error" if frame.get("error") else "done"
                if frame.get("error"):
                    exchange.error = text or "agent error"
                exchange.updated = now
                self.state.add_message(
                    character, "hermes", id=exchange.id, text=exchange.reply, t=now, status=exchange.status
                )
                self._raise_sig(exchange)
                if self.overlay is not None and hasattr(self.overlay, "note_reply"):
                    self.overlay.note_reply(character, exchange.reply, status=exchange.status, final=True)
                if self.overlay is not None and hasattr(self.overlay, "set_typing"):
                    self.overlay.set_typing(character, False)
            self.state.put_exchange(exchange)
            return

        self._log(f"unknown outbox kind {kind!r}")

    def _command_services(self, character: str, session: str) -> commands.Services:
        return commands.Services(
            character=character,
            session=session,
            confirms=self.confirms,
            addons_dir=self.config.addons_dir,
            staging_dir=self.config.staging_dir,
            ledger_path=self.config.addons_ledger_path,
            downloads_dir=self.config.downloads_dir,
            clock=self.clock,
            fetcher=self.fetcher,
            game_running=self._game_running,
            opener=self._opener,
        )

    def _handle_command(self, character: str, frame: dict) -> None:
        """PROTOCOL C `command` (v1.1/v1.2): validate, propose or execute, report a notice.

        The allowlist lives in ``commands.py``; nothing here interpolates a URL into a
        shell or filesystem path. Anything that writes to disk is parked for the player's
        in-game Accept first (:meth:`_handle_confirm` runs it).
        """
        services = self._command_services(character, self.state.session(character))
        result = commands.run(frame, services, opener=self._opener)
        command_id = str(frame.get("id") or "?")
        if result.pending:
            self._log(f"{character}: command {command_id} {result.action} awaiting player confirm")
        elif result.ok:
            self._log(f"{character}: command {command_id} {result.action} ok: {result.detail}")
        else:
            self._log(f"{character}: command {command_id} {result.action} refused: {result.reason}")
        self._publish_notice(character, result.notice, in_game=result.pending or result.ok)

    def _handle_confirm(self, character: str, session: str, text: str) -> None:
        """PROTOCOL A.1 `c`: execute a parked command only on a matching Accept.

        Accepted confirmations are single-use, same character, same session token, and no
        older than 10 minutes. Everything else (wrong session, expired, replayed, Cancel)
        writes nothing at all.
        """
        answer = commands.parse_confirm(text)
        if answer is None:
            self._log(f"{character}: malformed c record {text[:120]!r}")
            return
        taken = self.confirms.take(character, session, answer.command_id, now=self.clock())
        if taken.pending is None:
            reason = {
                "expired": "that confirmation expired, so nothing was installed",
                "session_mismatch": "that confirmation came from an older game session, so nothing was installed",
                "unknown": "nothing is waiting for that confirmation any more",
            }.get(taken.reason, "nothing was executed")
            self._log(f"{character}: confirm {answer.command_id} ignored ({taken.reason})")
            self._publish_notice(character, f"Confirmation ignored: {reason}.", in_game=True)
            return
        pending = taken.pending
        if not answer.ok:
            self._log(f"{character}: confirm {answer.command_id} cancelled by the player")
            verb = "removed" if pending.action == "addon.uninstall" else "installed"
            self._publish_notice(
                character,
                f"Cancelled — {pending.label} was not {verb}, and nothing was changed.",
                in_game=True,
            )
            return

        self._log(f"{character}: confirm {answer.command_id} accepted; running {pending.action}")
        services = self._command_services(character, session)
        try:
            result = commands.execute(pending.frame, services)
        except Exception as exc:  # noqa: BLE001 - a failed action must never kill the loop
            self._log(f"{character}: {pending.action} failed: {type(exc).__name__}: {exc}")
            result = commands.CommandResult(
                False,
                pending.action,
                reason="failed",
                message=(
                    f"{pending.label}: the action failed ({type(exc).__name__}). "
                    "Nothing else was changed."
                ),
            )
        if result.ok:
            self._log(f"{character}: {pending.action} ok: {result.detail}")
            # Only a real install success offers the in-game countdown.
            self._note_reload(character, result)
        else:
            self._log(f"{character}: {pending.action} refused after confirm: {result.reason}")
        self._publish_notice(character, result.notice, in_game=True)

    def _publish_notice(self, character: str, text: str, *, in_game: bool = False) -> None:
        """The bridge's notice channel (same surface as a gateway `notice` frame).

        ``in_game=True`` also carries the line to the addon (slot key ``note``) so the
        player sees the outcome without opening the overlay.
        """
        self._log(f"gateway notice: {text}")
        if self.overlay is not None and hasattr(self.overlay, "note_notice"):
            self.overlay.note_notice(character, text)
        if in_game and character:
            self._notices[character] = text
            self.publish(force=True)

    def _note_reload(self, character: str, result: commands.CommandResult) -> None:
        """Publish a post-install / post-removal reload prompt (PROTOCOL B.2 ``reload``).

        Only a **successful** ``addon.install`` or ``addon.uninstall`` gets one: Accept alone
        and every failure path publish nothing, so the addon never offers a reload that
        would not help.

        ``enable`` / ``disable`` ride along inside the same slot key (not as new keys): the
        addon calls ``C_AddOns.EnableAddOn`` or ``C_AddOns.DisableAddOn`` for those folders
        before showing the reminder, so the very ``/reload`` it asks for loads a new addon
        instead of leaving it disabled — or drops a removed one from the AddOns list.
        """
        if not character or result.action not in ("addon.install", "addon.uninstall") or not result.ok:
            return
        now = self.clock()
        name = str(result.data.get("name") or "").strip()[:120] or "Your addon"
        entry: dict = {
            "name": name,
            "ts": now,
            "exp": now + RELOAD_PROMPT_SECONDS,
        }
        for key in ("enable", "disable"):
            raw = result.data.get(key)
            if not isinstance(raw, (list, tuple)):
                continue
            names: list[str] = []
            for value in raw:
                if not isinstance(value, str):
                    continue
                folder = value.strip()[:120]
                if folder and folder not in names:
                    names.append(folder)
                if len(names) >= addons_pkg.MAX_ENABLE:
                    break
            if names:
                entry[key] = names
        self._reloads[character] = entry

    def _fresh_reload(self, character: str, *, now: float) -> dict | None:
        """The reload prompt still inside its window, else ``None`` (and drop it)."""
        entry = self._reloads.get(character)
        if entry is None:
            return None
        if float(entry.get("exp") or 0) <= now:
            self._reloads.pop(character, None)
            return None
        return dict(entry)

    def _expire_confirms(self) -> None:
        """A proposal older than 10 min is dead: say so, install nothing (research 08 §8.2)."""
        for pending in self.confirms.expire(now=self.clock()):
            self._log(f"{pending.character}: confirm {pending.command_id} expired (not_confirmed)")
            verb = "removed" if pending.action == "addon.uninstall" else "installed"
            self._publish_notice(
                pending.character,
                f"{pending.label}: not confirmed within 10 minutes, so nothing was {verb}.",
                in_game=True,
            )

    def _raise_sig(self, exchange: Exchange) -> None:
        """Queue the reply doorbell; it rings only once the priority window is on disk."""
        if exchange.synthetic:
            return
        if not self._sound_available(exchange.character):
            return
        self._pending_sigs[exchange.id] = exchange

    def _release_sigs(self) -> None:
        """Ring the doorbells queued by `done` — called after publish() returned.

        PROTOCOL B.2: sig is raised only after the priority window is on disk, so the
        slot the addon loads on the ring already carries the reply.
        """
        pending, self._pending_sigs = self._pending_sigs, {}
        for frame_id, exchange in sorted(pending.items()):
            self.bells.set("sig", frame_id)
            self._sig_ids.add(frame_id)
            exchange.seen_by_addon = False

    def _clear_sigs_up_to(self, frame_id: int) -> None:
        for raised in sorted(self._sig_ids):
            if raised <= frame_id:
                self.bells.clear("sig", raised)
                self._sig_ids.discard(raised)
                exchange = next((e for e in self.state.exchanges.values() if e.id == raised), None)
                if exchange is not None:
                    exchange.seen_by_addon = True

    def poll_outbox(self, character: str, wait: float = 0) -> int:
        if self.gateway is None:
            return 0
        current = self.state.since(character)
        raw = self.gateway.get_outbox(character, since=current, wait=wait)
        if isinstance(raw, OutboxBatch):
            batch = raw
        elif isinstance(raw, dict):
            batch = OutboxBatch(
                character=str(raw.get("character") or character),
                next=int(raw.get("next", current)),
                messages=list(raw.get("messages") or []),
                reset=bool(raw.get("reset")),
            )
        else:  # duck-typed test/alternate gateway
            batch = OutboxBatch(
                character=str(getattr(raw, "character", character) or character),
                next=int(getattr(raw, "next", current)),
                messages=list(getattr(raw, "messages", []) or []),
                reset=bool(getattr(raw, "reset", False)),
            )
        # Persist BEFORE handling the frames (PROTOCOL C backpressure). On a reset this
        # rewinds the stale cursor so the next poll asks for a sequence that exists.
        self.state.set_since(character, resolve_outbox_cursor(current, batch))
        if not batch.messages:
            return 0
        return self.handle_outbox_frames(character, batch.messages)

    # ---------------------------------------------------------------- reload mode
    def poll_savedvars(self) -> int:
        """PROTOCOL A.5: the addon's SavedVariables outbox, used when the strip is off."""
        path = self.config.savedvars_path
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return 0
        if self._savedvars_mtime == mtime:
            return 0
        self._savedvars_mtime = mtime
        try:
            db = parse_file(path).get("WoWHermesDB") or {}
        except LuaParseError as exc:
            self._log(f"SavedVariables parse failed: {exc}")
            return 0
        session = str(db.get("session") or "")
        character = self.character or str(db.get("char") or "") or self._character_for({"session": session})
        entries = outbox_entries(db, ack=self.state.ack(character))
        for entry in entries:
            record = {
                "session": session,
                "chat": str(entry.get("chat") or "c0"),
                "id": int(entry.get("id") or 0),
                "kind": str(entry.get("kind") or "m"),
                "flags": "",
                "text": str(entry.get("text") or ""),
            }
            self.handle_record(record)
        if entries:
            self._log(f"reload mode: {len(entries)} outbox entr{'y' if len(entries) == 1 else 'ies'}")
        return len(entries)

    # ---------------------------------------------------------------- publishing
    def publish(self, bridge: str = "up", *, force: bool = False) -> bool:
        character = self.character
        if not character:
            return False
        now = self.clock()
        if not force and now - self._last_publish < PUBLISH_MIN_INTERVAL:
            return False
        restore = None
        if self._restore and self._restore_publishes > 0:
            restore = self._restore
        pending = self.confirms.newest(character, now=now)
        state = build_slot_state(
            self.state,
            character,
            bridge=bridge,
            now=now,
            restore=restore,
            confirm=pending.to_slot() if pending else None,
            note=self._notices.get(character),
            reload=self._fresh_reload(character, now=now),
            boot=self.boot,
        )
        next_slot = self.state.next_slot(character)
        try:
            result = self.publisher.publish(state, next_slot=next_slot)
        except (SlotsNotInstalled, OSError) as exc:
            self.health.last_error = str(exc)
            self._log(f"publish failed: {exc}")
            return False
        self._generation += 1
        self._last_publish = now
        self._window = result.window
        if restore:
            self._restore_publishes -= 1
            if self._restore_publishes <= 0:
                self._restore = None
        if self.overlay is not None and hasattr(self.overlay, "set_health"):
            self.overlay.set_health(self.health.line())
        self._clear_old_acks()
        self.state.save()
        return True

    def _clear_old_acks(self) -> None:
        """PROTOCOL B.3: an ack lives >= 2 s *and* one publish, at most 60 s."""
        now = self.clock()
        stale = [
            frame_id
            for frame_id, (raised_at, generation) in self._acks_raised.items()
            if now - raised_at >= ACK_MAX_LIFETIME
            or (now - raised_at >= ACK_MIN_LIFETIME and self._generation > generation)
        ]
        for frame_id in stale:
            self.bells.clear("ack", frame_id)
            self._acks_raised.pop(frame_id, None)

    # ---------------------------------------------------------------- threads
    def ensure_poller(self, character: str | None = None) -> OutboxPoller | None:
        character = character or self.character
        if not character or self.client is None or self._config_error:
            return None
        existing = self._pollers.get(character)
        if existing and existing.is_alive():
            return existing
        poller = OutboxPoller(
            self.client,
            character,
            self.state.since(character),
            lambda frames, ch=character: self.handle_outbox_frames(ch, frames),
            on_cursor=lambda cursor, ch=character: self.state.set_since(ch, cursor),
            stop_event=self._stop,
            on_error=lambda exc: self._note_config_error(exc) if isinstance(exc, ConfigError) else None,
            logger=self._log,
        )
        poller.start()
        self._pollers[character] = poller
        return poller

    def run(self, stop_event: threading.Event | None = None) -> None:  # pragma: no cover - shell
        stop = stop_event or self._stop
        self.start()
        try:
            while not stop.is_set():
                try:
                    self.tick()
                    self.poll_savedvars()
                    if self.character and self.client is not None:
                        self.ensure_poller(self.character)
                except Exception:  # noqa: BLE001
                    self._log("tick failed:\n" + traceback.format_exc())
                stop.wait(self.next_interval())
        finally:
            for poller in self._pollers.values():
                poller.stop_event.set()
            self.publish(bridge="down", force=True)
            # Let the last background pass finish so a clean shutdown does not leave the
            # pool half-written.
            self.publisher.wait_for_background(5.0)

    def stop(self) -> None:
        self._stop.set()


@dataclass
class StatusSnapshot:
    character: str
    health: str
    exchanges: int
    pending: int
    slots_installed: int
    sound_mode: str
    since: int
    ack: int
    details: dict = field(default_factory=dict)
