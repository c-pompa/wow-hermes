"""WoW (World of Warcraft: Forever) platform adapter.

Wire contract: ``docs/PROTOCOL.md`` section C. The companion bridge — a Python app
running next to the game client — speaks HTTP to this adapter inside the gateway
process:

* ``POST /v1/wow/message`` — one chat line in from the game (bearer token).
* ``GET  /v1/wow/outbox?character=&since=&wait=`` — long-poll out for ``reply``,
  ``typing``, ``done`` (and future ``notice``) frames.
* ``GET  /v1/wow/health`` — liveness; unauthenticated on loopback only.

Each character key (``Name-Realm``) is one Hermes DM session
(``<ns>:wow:dm:Name-Realm``), so ``chat_id == user_id == character``.

Config (``config.yaml`` ``gateway.platforms.wow.extra``), env wins over YAML, and
every read goes through the profile-scoped helpers (multiplex rule — never
``os.getenv`` here): ``host``/``WOW_HOST`` (default ``127.0.0.1``), ``port``/
``WOW_PORT`` (default ``8791``), ``bridge_token``/``WOW_BRIDGE_TOKEN``.
Authorization: ``WOW_ALLOWED_USERS`` (comma list) / ``WOW_ALLOW_ALL_USERS``,
enforced here at intake *and* by the gateway core through ``allowed_users_env``.

Turn completion: ``done`` frames are emitted from
``BasePlatformAdapter.on_processing_complete`` (``gateway/platforms/base.py:3418``),
which the base class runs after the final ``send()`` of a turn
(``base.py:4546``). See ``docs/notes/ws-plugin.md``.

Reliability (WS-N, ``docs/notes/ws-reliability.md``):

* Outbox cursors are per adapter process. A ``since`` cursor above this incarnation's
  ``next_seq`` is stale (the bridge kept the previous process's cursor), so the outbox
  replays its retained frames and answers ``"reset": true`` with a cursor that exists here.
* ``WOW_RESPONSE_TIMEOUT_SECONDS`` (default 300 s, min 30) bounds the per-inbound
  ``handle_message`` dispatch. Real async work owned by the same session extends the
  deadline; otherwise the turn is cancelled with a clear reply plus ``done``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hmac
import importlib
import importlib.util
import inspect
import json
import logging
import re
import secrets
import sys
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

try:  # passive dependency probe must never raise
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except Exception:  # pragma: no cover - aiohttp ships with Hermes
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import (
    coerce_port,
    extra_or_secret as _extra_or_secret,
    get_scoped_secret as _get_scoped_secret,
    platform_gate_env as _platform_gate_env,
    seed_extra_from_env as _seed_extra_from_env,
    send_error,
)
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome
from gateway.platforms.helpers import MessageDeduplicator

logger = logging.getLogger(__name__)

# ── defaults / limits ────────────────────────────────────────────────────────
DEFAULT_HOST = "127.0.0.1"       # explicit LAN/VPN bind is a deployment decision
DEFAULT_PORT = 8791
MAX_BODY_BYTES = 64 * 1024       # PROTOCOL C: bodies <= 64 KiB
MAX_MESSAGE_LENGTH = 4000        # outbox frame cap; the in-game window wraps text
MAX_WAIT_SECONDS = 25.0          # PROTOCOL C: wait is 0..25
RETENTION_FRAMES = 512           # per character
RETENTION_SECONDS = 24 * 3600.0
DEDUP_WINDOW_SECONDS = 3600
DEDUP_MAX_SIZE = 5000
TYPING_MIN_INTERVAL = 3.0        # seconds between typing frames per character
MAX_CONTEXT_FIELDS = 8
# A turn that has not finished within this many seconds is a stuck turn — unless it
# really owns live async work (see ``active_work_for_session``), in which case the
# deadline is extended instead of cancelling real work.
DEFAULT_RESPONSE_TIMEOUT_SECONDS = 300.0
MIN_RESPONSE_TIMEOUT_SECONDS = 30.0
RESPONSE_TIMEOUT_ENV = "WOW_RESPONSE_TIMEOUT_SECONDS"
TIMEOUT_EXTENSION_TEXT = "Still working on that…"
# Never extend a genuinely active turn forever: a runaway delegation must still end.
MAX_RESPONSE_EXTENSIONS = 20
TIMED_OUT_TTL_SECONDS = 3600.0   # how long a timed-out turn keeps its "no late output" guard
TIMED_OUT_MAX_SIZE = 512
LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"})

# ``Name-Realm``. WoW character names are 2-12 letters (no digits/underscore);
# realms may carry digits, apostrophes and hyphens (a realm with spaces is
# normalised to hyphens at intake, e.g. "Area 52" -> "Area-52").
# The canonical implementation lives in ``modes.py`` (shared with the backend);
# this module re-exports it.

BRIDGE_TOKEN_ENV = "WOW_BRIDGE_TOKEN"
HOST_ENV = "WOW_HOST"
PORT_ENV = "WOW_PORT"
ALLOWED_USERS_ENV = "WOW_ALLOWED_USERS"
ALLOW_ALL_ENV = "WOW_ALLOW_ALL_USERS"
HOME_CHANNEL_ENV = "WOW_HOME_CHANNEL"

PLATFORM_HINT = (
    "You are talking to a World of Warcraft: Forever player through an in-game "
    "window and a desktop overlay. Text arrives as plain chat lines; replies are "
    "read while the client keeps running, so answer briefly (a few short "
    "paragraphs at most), in plain text — no markdown tables, no code fences, no "
    "attachments, unless the player explicitly asks for them. Long output should "
    "be summarised in the reply and written to a file instead. The player may be "
    "mid-fight: lead with the answer, then the detail."
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})


# ── live adapter registry (status + out-of-turn frames) ─────────────────────
# One adapter per gateway process in practice; a multiplexed host may run one per
# profile, so keep the most recently connected and a per-key map for status.
_LIVE_ADAPTERS: dict[str, "WoWAdapter"] = {}
_LAST_ADAPTER: Optional["WoWAdapter"] = None
_LAST_SEEN: dict[str, float] = {}
_LAST_CLIENT: dict[str, dict[str, Any]] = {}


def active_adapter() -> Optional["WoWAdapter"]:
    """The most recently connected adapter in this process, or ``None``."""
    adapter = _LAST_ADAPTER
    if adapter is not None and adapter.is_connected:
        return adapter
    for candidate in reversed(list(_LIVE_ADAPTERS.values())):
        if candidate.is_connected:
            return candidate
    return None


def status_snapshot() -> dict[str, Any]:
    """Process-local status for ``GET /api/plugins/wow/status`` (research 07 §3.4).

    Never raises and never leaks the bearer token — only whether one is set.
    """
    adapter = active_adapter()
    characters = sorted(set(_LAST_SEEN) | set(_LAST_CLIENT))
    modes_by_character: dict[str, str] = {}
    if adapter is not None:
        try:
            modes_by_character = adapter.mode_snapshot().get("characters") or {}
        except Exception:
            modes_by_character = {}
    return {
        "listening": bool(adapter is not None and adapter.is_connected),
        "bound": (f"{adapter._host}:{adapter._bound_port}" if adapter is not None else ""),
        "token_set": bool(adapter is not None and adapter._bridge_token),
        "characters": characters,
        "last_seen": _LAST_SEEN.get(characters[0]) if len(characters) == 1 else dict(_LAST_SEEN),
        "last_seen_by_character": dict(_LAST_SEEN),
        "last_bridge_hello": next(iter(_LAST_CLIENT.values()), None),
        "modes": modes_by_character,
    }


_COMMAND_COUNTER = 0


def next_command_id() -> str:
    """Bridge command id, ``c<N>`` (PROTOCOL C example ``"id":"c7"``)."""
    global _COMMAND_COUNTER
    _COMMAND_COUNTER += 1
    return f"c{_COMMAND_COUNTER}"


def enqueue_frame(character: str, frame: dict[str, Any]) -> bool:
    """Push one complete outbox frame (``command``/``notice``) for *character*.

    Returns False when no live adapter can take it. Frames are never logged
    with their payload here — ``addon.install`` carries a write-only API key.
    """
    adapter = active_adapter()
    character = normalize_character(character) or str(character or "").strip()
    if adapter is None or not character:
        return False
    payload = dict(frame)
    payload.setdefault("id", next_command_id())
    adapter._outbox(character).append_frame(payload)
    return True


def enqueue_command(character: str, action: str, url: str) -> bool:
    """Push an allowlisted ``command`` frame (PROTOCOL C: ``open.url`` only).

    Used by ``wow_addon_guide`` so a CurseForge install link opens on the game PC.
    Returns False when no live adapter/outbox can take it.
    """
    return enqueue_frame(character, {"kind": "command", "action": action, "url": url})



def _error_response(error: str, status: int) -> "web.Response":
    return web.json_response({"ok": False, "error": error}, status=status)


# ── helpers ──────────────────────────────────────────────────────────────────


def _load_sibling(name: str):
    """Import a sibling module of this package, whichever way we were loaded.

    In the gateway the plugin is a real package (``wow``), so a relative import
    works. The repo's tests load ``adapter.py`` by path under a synthetic module
    name with no package around it, so fall back to a file-location import of
    ``<dir>/<name>.py`` cached under a stable private name.
    """
    if __package__:
        try:
            return importlib.import_module(f"{__package__}.{name}")
        except ImportError:
            pass
    cache_key = f"_wow_plugin_{name}"
    cached = sys.modules.get(cache_key)
    if cached is not None:
        return cached
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(cache_key, path)
    if spec is None or spec.loader is None:  # pragma: no cover - impossible for a real file
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[cache_key] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(cache_key, None)
        raise
    return module


modes = _load_sibling("modes")
normalize_character = modes.normalize_character


def context_line(context: Any) -> str:
    """One bracketed log line describing the game context, or ``""``.

    ``{"zone": "Elwynn Forest", "lvl": 7}`` -> ``[WoW context: zone=Elwynn Forest; lvl=7]``.
    Values are flattened to a single line and capped; the bridge controls them, so
    nothing here is trusted beyond being inert text.
    """
    if not isinstance(context, dict) or not context:
        return ""
    parts: list[str] = []
    for key, value in context.items():
        if value is None or len(parts) >= MAX_CONTEXT_FIELDS:
            continue
        key_text = re.sub(r"\s+", " ", str(key)).strip()[:32]
        if not key_text:
            continue
        value_text = re.sub(r"[\r\n]+", " ", str(value)).strip()[:120]
        if not value_text:
            continue
        parts.append(f"{key_text}={value_text}")
    return f"[WoW context: {'; '.join(parts)}]" if parts else ""


def _floats(value: Any, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out and out not in (float("inf"), float("-inf")) else default


def resolve_response_timeout(extra: Optional[dict[str, Any]] = None) -> float:
    """``WOW_RESPONSE_TIMEOUT_SECONDS`` (env) → ``extra.response_timeout_seconds`` → 300 s.

    Clamped to at least 30 s: a smaller value turns ordinary provider latency into a
    cancellation storm. Tests override ``WoWAdapter._response_timeout`` directly so
    they can exercise the watchdog without sleeping.
    """
    raw = _extra_or_secret(extra or {}, "response_timeout_seconds", RESPONSE_TIMEOUT_ENV,
                           DEFAULT_RESPONSE_TIMEOUT_SECONDS)
    value = _floats(raw, DEFAULT_RESPONSE_TIMEOUT_SECONDS)
    if value < MIN_RESPONSE_TIMEOUT_SECONDS:
        logger.warning("[wow] %s=%r is below the %ds floor; using %ds",
                       RESPONSE_TIMEOUT_ENV, raw, int(MIN_RESPONSE_TIMEOUT_SECONDS),
                       int(MIN_RESPONSE_TIMEOUT_SECONDS))
        return MIN_RESPONSE_TIMEOUT_SECONDS
    return value


def active_work_for_session(session_key: str) -> list[str]:
    """Reasons a gateway session still owns *real* background work; ``[]`` when none.

    Both sources are process-local registries, which is exactly right: the adapter and
    the tools run in the same gateway process.

    * ``tools.async_delegation.has_live_for_session(session_key=...)`` — live
      ``delegate_task``/subagent records. The registry matches records on any of
      ``origin_ui_session_id`` / ``session_key`` / ``parent_session_id``; for a gateway
      chat the platform conversation key is the one that survives ``/new``
      (``tools/delegate_tool_dispatch.py:290-310`` resolves it from the approval
      context, which the gateway binds to the same key at
      ``gateway/run_turn_runner.py:983``).
    * ``tools.process_registry.process_registry.has_active_for_session(session_key)`` —
      running background jobs/processes started from this session
      (``tools/process_registry.py:2472``).

    An empty key fails closed (no extension): without proof of ownership the watchdog
    must not keep waiting on someone else's work.
    """
    if not session_key:
        return []
    reasons: list[str] = []
    try:
        from tools.async_delegation import has_live_for_session

        if has_live_for_session(session_key=session_key):
            reasons.append("subagent")
    except Exception:  # pragma: no cover - registry availability is a core detail
        logger.debug("[wow] async-delegation probe failed", exc_info=True)
    try:
        from tools.process_registry import process_registry

        if process_registry.has_active_for_session(session_key):
            reasons.append("job")
    except Exception:  # pragma: no cover - registry availability is a core detail
        logger.debug("[wow] process-registry probe failed", exc_info=True)
    return reasons


class _CharacterOutbox:
    """Per-character frame ring plus the wake-up event long-pollers await."""

    def __init__(self) -> None:
        self.frames: deque[dict[str, Any]] = deque(maxlen=RETENTION_FRAMES)
        self.next_seq = 1
        self._wakeup = asyncio.Event()

    def append(self, kind: str, *, text: Optional[str] = None,
               reply_to: Optional[str] = None, ttl_ms: Optional[int] = None) -> int:
        frame: dict[str, Any] = {"kind": kind}
        if text is not None:
            frame["text"] = text
        if reply_to is not None:
            frame["reply_to"] = reply_to
        if ttl_ms is not None:
            frame["ttl_ms"] = ttl_ms
        return self.append_frame(frame)

    def append_frame(self, frame: dict[str, Any]) -> int:
        """Append a full frame (``command``/``notice`` carry extra fields, PROTOCOL C)."""
        now = time.time()
        seq = self.next_seq
        self.next_seq += 1
        stored: dict[str, Any] = {"seq": seq, "kind": str(frame.get("kind") or ""), "ts": now}
        for key, value in frame.items():
            if key in {"seq", "ts", "kind"} or value is None:
                continue
            stored[key] = value
        self.frames.append(stored)  # deque(maxlen) drops the oldest frame
        # Retain at most 24 h of frames after the count cap.
        cutoff = now - RETENTION_SECONDS
        while self.frames and self.frames[0]["ts"] < cutoff:
            self.frames.popleft()
        self.wake()
        return seq

    def frames_after(self, cursor: int) -> list[dict[str, Any]]:
        return [frame for frame in self.frames if frame["seq"] > cursor]

    def wake(self) -> None:
        """Wake every waiter and hand the next poller a fresh, unset event."""
        self._wakeup.set()
        self._wakeup = asyncio.Event()

    def wakeup(self) -> asyncio.Event:
        return self._wakeup


class WoWAdapter(BasePlatformAdapter):
    """HTTP in / long-poll out adapter for the WoW companion bridge."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    # The gateway router chunks long answers with ``truncate_message`` before
    # calling ``send()`` (each chunk becomes its own reply frame), so we do not
    # advertise in-send splitting.
    splits_long_messages = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("wow"))
        extra = config.extra or {}
        self._host: str = str(_extra_or_secret(extra, "host", HOST_ENV, DEFAULT_HOST)).strip() or DEFAULT_HOST
        self._port: int = coerce_port(_extra_or_secret(extra, "port", PORT_ENV, DEFAULT_PORT), DEFAULT_PORT)
        # ``or ""``: a null YAML value must reach connect()'s auto-generated token.
        self._bridge_token: str = str(_extra_or_secret(extra, "bridge_token", BRIDGE_TOKEN_ENV, "") or "").strip()
        self._runner: Optional[web.AppRunner] = None
        self._bound_port: int = 0
        self._outboxes: dict[str, _CharacterOutbox] = {}
        self._last_inbound: dict[str, str] = {}
        self._last_typing: dict[str, float] = {}
        self._dedup = MessageDeduplicator(max_size=DEDUP_MAX_SIZE, ttl_seconds=DEDUP_WINDOW_SECONDS)
        self._stopping = asyncio.Event()
        # Response watchdog (WS-N): a stuck turn is cancelled after this many seconds
        # unless ``active_work_for_session`` proves real async work for the same session.
        self._response_timeout: float = resolve_response_timeout(extra)
        self._max_extensions: int = MAX_RESPONSE_EXTENSIONS
        # msg_id -> monotonic stamp of a turn the watchdog already terminated. Late
        # output from its (possibly uncancellable) dispatch is dropped, so the player
        # never gets a timeout notice followed by a duplicate answer.
        self._timed_out: dict[str, float] = {}
        # Per-character modes (v1.1). The store is file-backed under the plugin
        # data dir so a gateway reload keeps every character's mode.
        self._modes = modes.ModeStore(full_ttl_seconds=modes.resolve_full_ttl_seconds())
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._expiry_handles: dict[str, asyncio.TimerHandle] = {}

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not AIOHTTP_AVAILABLE:
            logger.warning("[wow] aiohttp is not importable; cannot start the bridge endpoint")
            return False
        if not self._bridge_token:
            self._bridge_token = secrets.token_hex(32)
            logger.info(
                "[wow] WOW_BRIDGE_TOKEN unset — generated an in-memory bearer token for this "
                "process (set it explicitly so the bridge can be configured)")
        # One endpoint per host:port across every profile/gateway on this machine.
        if not self._acquire_platform_lock("wow-bridge", f"{self._host}:{self._port}",
                                           f"WoW bridge endpoint {self._host}:{self._port}"):
            return False
        # client_max_size makes aiohttp enforce the body cap on every read path,
        # including chunked bodies with no Content-Length (raft/webhook pattern).
        app = web.Application(client_max_size=MAX_BODY_BYTES)
        app.router.add_get("/v1/wow/health", self._handle_health)
        app.router.add_post("/v1/wow/message", self._handle_message_post)
        app.router.add_get("/v1/wow/outbox", self._handle_outbox)
        self._stopping = asyncio.Event()
        try:
            self._runner = web.AppRunner(app)
            await self._runner.setup()
            site = web.TCPSite(self._runner, self._host, self._port)
            await site.start()
        except OSError as exc:
            logger.error("[wow] Cannot bind %s:%s — %s", self._host, self._port, exc)
            await self._cleanup_runner()
            self._release_platform_lock()
            return False
        bound = self._port
        server = getattr(site, "_server", None)
        sockets = getattr(server, "sockets", None)
        if bound == 0 and sockets:
            bound = sockets[0].getsockname()[1]
        self._bound_port = bound
        self._port = bound or self._port
        self._mark_connected()
        self._wire_plugin_handlers(None)  # plugin-registered native handlers
        global _LAST_ADAPTER
        _LAST_ADAPTER = self
        _LIVE_ADAPTERS[f"{self._host}:{self._bound_port}"] = self
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - connect always runs on the loop
            self._loop = None
        self._reschedule_expiries()
        logger.info("[wow] Bridge endpoint listening on http://%s:%d/v1/wow/", self._host, self._bound_port)
        return True

    async def disconnect(self) -> None:
        self._stopping.set()
        for handle in self._expiry_handles.values():
            handle.cancel()
        self._expiry_handles.clear()
        for outbox in self._outboxes.values():
            outbox.wake()  # release long-pollers instead of letting cleanup time them out
        await self._cleanup_runner()
        self._release_platform_lock()
        self._mark_disconnected()
        _LIVE_ADAPTERS.pop(f"{self._host}:{self._bound_port}", None)
        self._dedup.clear()
        self._timed_out.clear()
        logger.info("[wow] Disconnected")

    async def _cleanup_runner(self) -> None:
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()
        self._bound_port = 0

    # ── HTTP handlers ────────────────────────────────────────────────────────

    def _authorized(self, request: "web.Request") -> bool:
        if not self._bridge_token:
            return False
        header = request.headers.get("Authorization", "") or ""
        token = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
        if not token:
            return False
        # Compare as bytes: compare_digest raises TypeError on a non-ASCII str.
        return hmac.compare_digest(token.encode("utf-8", "replace"), self._bridge_token.encode("utf-8", "replace"))

    def _is_loopback(self, request: "web.Request") -> bool:
        return (getattr(request, "remote", "") or "") in LOOPBACK_ADDRESSES

    def _character_allowed(self, character: str) -> bool:
        """Adapter-side allowlist gate.

        Mirrors the gateway's own ``WOW_ALLOWED_USERS``/``WOW_ALLOW_ALL_USERS`` check
        so the bridge gets a definitive "do not expect a reply" (202, accepted false)
        instead of an inbound that the core then silently drops.
        """
        if _platform_gate_env(ALLOW_ALL_ENV, "").strip().lower() in _TRUTHY:
            return True
        raw = _platform_gate_env(ALLOWED_USERS_ENV, "")
        allowed = {entry.strip().lower() for entry in raw.split(",") if entry.strip()}
        if not allowed:
            return True  # no allowlist configured — the core's authorization ladder decides
        normalized = {normalize_character(entry) or entry.strip().lower() for entry in allowed}
        return character.lower() in normalized

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        # Loopback liveness probes (and container health checks) need no token;
        # anything arriving over the LAN must authenticate like every other route.
        # A supplied bearer is always validated (PROTOCOL C), so a misconfigured bridge
        # learns about a bad token from its first health probe even over loopback.
        supplied = bool(request.headers.get("Authorization"))
        if (supplied or not self._is_loopback(request)) and not self._authorized(request):
            return _error_response("unauthorized", 401)
        return web.json_response({
            "ok": True,
            "platform": "wow",
            "version": 1,
            "connected": self.is_connected,
            "characters": sorted(self._outboxes),
        })

    async def _read_body(self, request: "web.Request") -> tuple[Optional[bytes], Optional["web.Response"]]:
        if (request.content_length or 0) > MAX_BODY_BYTES:
            return None, _error_response("payload_too_large", 413)
        try:
            raw = await request.read()
        except web.HTTPRequestEntityTooLarge:
            return None, _error_response("payload_too_large", 413)
        except Exception:  # pragma: no cover - client aborted mid-body
            return None, _error_response("bad_request", 400)
        if len(raw) > MAX_BODY_BYTES:  # defence in depth if the server cap was bypassed
            return None, _error_response("payload_too_large", 413)
        return raw, None

    async def _handle_message_post(self, request: "web.Request") -> "web.Response":
        if not self._authorized(request):
            return _error_response("unauthorized", 401)
        raw, error = await self._read_body(request)
        if error is not None:
            return error
        try:
            payload = json.loads(raw) if raw.strip() else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error_response("invalid_json", 400)
        if not isinstance(payload, dict):
            return _error_response("invalid_payload", 400)

        character = normalize_character(payload.get("character"))
        if not character:
            return _error_response("invalid_character", 400)
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return _error_response("invalid_text", 400)
        text = text.strip()
        msg_id = payload.get("msg_id")
        if not isinstance(msg_id, str) or not msg_id.strip() or len(msg_id) > 128:
            return _error_response("invalid_message_id", 400)
        msg_id = msg_id.strip()

        if not self._character_allowed(character):
            logger.info("[wow] Character %s is not in %s — not accepted", character, ALLOWED_USERS_ENV)
            return web.json_response({"ok": True, "accepted": False, "reason": "unauthorized"}, status=202)
        if self._dedup.is_duplicate(msg_id):
            logger.debug("[wow] Duplicate frame %s from %s, not accepted", msg_id, character)
            return web.json_response({"ok": True, "accepted": False, "reason": "duplicate"}, status=202)

        ts = _floats(payload.get("ts"), time.time())
        try:
            timestamp = datetime.fromtimestamp(ts, tz=UTC)
        except (OverflowError, OSError, ValueError):
            timestamp = datetime.now(tz=UTC)
        ctx = context_line(payload.get("context"))
        event_text = f"{ctx}\n{text}" if ctx else text

        source = self.build_source(
            chat_id=character, chat_name=character, chat_type="dm",
            user_id=character, user_name=character, message_id=msg_id)
        event = MessageEvent(
            text=event_text, message_type=MessageType.TEXT, source=source,
            message_id=msg_id, raw_message=payload, timestamp=timestamp)
        # The reply anchor for this turn: send() stamps it on every reply frame so
        # the bridge can match an answer to the message that caused it.
        self._last_inbound[character] = msg_id
        _LAST_SEEN[character] = time.time()
        client = payload.get("client")
        if isinstance(client, dict):
            _LAST_CLIENT[character] = {
                "character": character,
                "ts": time.time(),
                "build": str(client.get("build") or "")[:64],
                "interface": client.get("interface"),
            }
        logger.debug("[wow] Inbound from %s (%s): %s", character, msg_id, text[:80])
        try:
            timed_out = await self._dispatch_with_watchdog(character, msg_id, event)
        except Exception:
            logger.exception("[wow] Failed to dispatch inbound frame from %s", character)
            # Release the claim so a bridge retry of the same frame is accepted.
            self._dedup.discard(msg_id)
            return _error_response("dispatch_failed", 503)
        if timed_out is not None:
            return timed_out
        return web.json_response({"ok": True, "accepted": True}, status=202)

    # ── response watchdog ────────────────────────────────────────────────────

    async def _invoke_handle_message(self, event: MessageEvent) -> None:
        """Await ``handle_message`` even when a test/gateway hook returns a plain value."""
        result = self.handle_message(event)
        if inspect.isawaitable(result):
            await result

    def _turn_session_key(self, event: MessageEvent) -> str:
        """The platform conversation key async work for this turn is stamped with.

        ``gateway/run_turn_runner.py:983`` binds the same key into the approval context
        that ``tools/delegate_tool_dispatch.py:290-310`` reads, so registry lookups
        match. Best-effort: an unreadable key fails closed (no extension).
        """
        try:
            return self._event_session_key(event) or ""
        except Exception:  # pragma: no cover - identity machinery is a core detail
            logger.debug("[wow] could not derive the session key for %r",
                         getattr(event, "message_id", None), exc_info=True)
            return ""

    def _mark_timed_out(self, msg_id: str) -> None:
        now = time.monotonic()
        for key, stamp in list(self._timed_out.items()):
            if now - stamp > TIMED_OUT_TTL_SECONDS:
                self._timed_out.pop(key, None)
        while len(self._timed_out) >= TIMED_OUT_MAX_SIZE:
            self._timed_out.pop(next(iter(self._timed_out)), None)
        self._timed_out[msg_id] = now

    def _is_timed_out(self, msg_id: Optional[str]) -> bool:
        if not msg_id:
            return False
        return str(msg_id) in self._timed_out

    def _notify_still_working(self, character: str, msg_id: str, reasons: list[str]) -> None:
        """Keep the in-game window alive while real work continues (no reply, no done)."""
        outbox = self._outbox(character)
        outbox.append("typing", ttl_ms=3000, reply_to=msg_id)
        outbox.append("notice", text=TIMEOUT_EXTENSION_TEXT, reply_to=msg_id)
        logger.info("[wow] Turn %s for %s is still running (%s) — extending the deadline",
                    msg_id, character, ", ".join(reasons))

    def _timeout_label(self) -> str:
        """``300`` for whole seconds, one decimal for a fractional (test) deadline."""
        seconds = float(self._response_timeout)
        return str(int(seconds)) if seconds.is_integer() else f"{seconds:.1f}"

    def _finish_timed_out(self, character: str, msg_id: str) -> "web.Response":
        """Answer the player, terminate the turn, and latch the late-output guard."""
        self._mark_timed_out(msg_id)
        self._last_typing.pop(character, None)
        outbox = self._outbox(character)
        label = self._timeout_label()
        outbox.append(
            "reply",
            text=(f"Hermes did not respond within {label}s, so I stopped waiting. "
                  "Try again, or use /new if the session seems stuck."),
            reply_to=msg_id)
        outbox.append("done", reply_to=msg_id)
        logger.warning("[wow] Turn %s for %s timed out after %ss with no live async work",
                       msg_id, character, label)
        # 202 accepted: the bridge must not retry this inbound forever. The dedup entry
        # stays, so a retry that ignores this is still rejected as a duplicate.
        return web.json_response(
            {"ok": True, "accepted": True, "timed_out": True,
             "timeout_seconds": (int(self._response_timeout)
                                 if float(self._response_timeout).is_integer()
                                 else float(self._response_timeout))},
            status=202)

    async def _dispatch_with_watchdog(self, character: str, msg_id: str,
                                      event: MessageEvent) -> Optional["web.Response"]:
        """Await the turn; extend while real async work runs, otherwise time out.

        Each round waits one ``_response_timeout`` interval. At the deadline:

        * live subagent/job work owned by this session → a typing/notice frame and
          another interval (never cancel real work);
        * nothing active → cancel the stuck dispatch, send the timeout reply + ``done``
          and answer 202 with ``timed_out`` so the bridge stops retrying.

        Returns ``None`` for a normal completion, or the timeout response.
        """
        session_key = self._turn_session_key(event)
        extensions = 0
        while True:
            task = asyncio.ensure_future(self._invoke_handle_message(event))
            done, _pending = await asyncio.wait({task}, timeout=self._response_timeout)
            if task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
                return None
            reasons = active_work_for_session(session_key) if extensions < self._max_extensions else []
            if reasons:
                extensions += 1
                self._notify_still_working(character, msg_id, reasons)
                continue
            task.cancel()
            # A dispatch that swallows CancelledError must not keep the request open.
            with contextlib.suppress(BaseException):
                await asyncio.wait({task}, timeout=2.0)
            if not task.done():  # pragma: no cover - a handler ignoring cancellation
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            return self._finish_timed_out(character, msg_id)

    async def _handle_outbox(self, request: "web.Request") -> "web.Response":
        if not self._authorized(request):
            return _error_response("unauthorized", 401)
        character = normalize_character(request.query.get("character"))
        if not character:
            return _error_response("invalid_character", 400)
        cursor = coerce_port(_floats(request.query.get("since", "0"), 0.0), 0)
        wait = min(max(_floats(request.query.get("wait", "0"), 0.0), 0.0), MAX_WAIT_SECONDS)

        outbox = self._outbox(character)
        # A cursor at/above this incarnation's next sequence number cannot exist here:
        # it was persisted against a previous adapter process whose sequence space began
        # at 1 again (WS-N incident: bridge kept since=21, reply landed at seq 6). Replay
        # the retained frames from the start, flag the reset, and answer immediately —
        # waiting out the long poll would leave the player unanswered forever.
        reset = cursor > 0 and cursor >= outbox.next_seq
        if reset:
            logger.warning("[wow] Stale outbox cursor %d for %s (next_seq=%d) — replaying from 0",
                           cursor, character, outbox.next_seq)
            frames = outbox.frames_after(0)
        else:
            frames = outbox.frames_after(cursor)
            if not frames and wait > 0 and not self._stopping.is_set():
                frames = await self._wait_for_frames(outbox, cursor, wait)
        messages = frames
        if reset:
            next_cursor = messages[-1]["seq"] if messages else 0
        else:
            next_cursor = messages[-1]["seq"] if messages else cursor
        body: dict[str, Any] = {"character": character, "next": next_cursor, "messages": messages}
        if reset:
            body["reset"] = True
        return web.json_response(body)

    async def _wait_for_frames(self, outbox: _CharacterOutbox, cursor: int, wait: float) -> list[dict[str, Any]]:
        """Block until a frame lands, the wait expires, or the adapter disconnects."""
        wakeup = outbox.wakeup()
        stopping = self._stopping
        wait_task = asyncio.ensure_future(wakeup.wait())
        stop_task = asyncio.ensure_future(stopping.wait())
        try:
            await asyncio.wait({wait_task, stop_task}, timeout=wait,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (wait_task, stop_task):
                if not task.done():
                    task.cancel()
        return outbox.frames_after(cursor)

    # ── outbound ─────────────────────────────────────────────────────────────

    def _outbox(self, character: str) -> _CharacterOutbox:
        outbox = self._outboxes.get(character)
        if outbox is None:
            outbox = _CharacterOutbox()
            self._outboxes[character] = outbox
        return outbox

    def _anchor_for(self, character: str, reply_to: Optional[str],
                    metadata: Optional[dict[str, Any]]) -> Optional[str]:
        explicit = (metadata or {}).get("reply_to") or reply_to
        if explicit:
            return str(explicit)
        return self._last_inbound.get(character)

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        character = normalize_character(chat_id) or str(chat_id or "").strip()
        if not character:
            return SendResult(success=False, error="wow: empty chat_id")
        anchor = self._anchor_for(character, reply_to, metadata)
        if self._is_timed_out(anchor):
            # The watchdog already answered this turn; a dispatch that outlived its
            # cancellation must not append a duplicate answer.
            logger.info("[wow] Dropping late reply for timed-out turn %s (%s)", anchor, character)
            return SendResult(success=False, error="wow: turn timed out")
        text = content or ""
        if len(text) > self.MAX_MESSAGE_LENGTH:
            logger.warning("[wow] Truncating reply from %d to %d chars", len(text), self.MAX_MESSAGE_LENGTH)
            text = text[: self.MAX_MESSAGE_LENGTH]
        seq = self._outbox(character).append(
            "reply", text=text, reply_to=anchor)
        return SendResult(success=True, message_id=f"wow-{character}-{seq}")

    async def send_typing(self, chat_id: str, metadata: Optional[dict[str, Any]] = None) -> None:
        character = normalize_character(chat_id) or str(chat_id or "").strip()
        if not character:
            return
        anchor = self._last_inbound.get(character)
        if self._is_timed_out(anchor):
            return
        now = time.monotonic()
        last = self._last_typing.get(character, 0.0)
        if now - last < TYPING_MIN_INTERVAL:
            return
        self._last_typing[character] = now
        self._outbox(character).append("typing", ttl_ms=3000, reply_to=anchor)

    async def stop_typing(self, chat_id: str) -> None:
        # No native typing surface: the overlay expires the frame on ``ttl_ms``.
        return None

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Emit the ``done`` frame that marks the end of an agent turn.

        ``BasePlatformAdapter._process_message_background`` runs this hook after the
        final ``send()`` (``gateway/platforms/base.py:4546``) and on failure/cancel
        (``:4564``, ``:4569``), so the bridge always sees a turn terminator.
        """
        source = getattr(event, "source", None)
        chat_id = getattr(source, "chat_id", None)
        if not chat_id:
            return
        anchor = getattr(event, "message_id", None)
        if self._is_timed_out(anchor):
            # The timeout path already emitted this turn's ``done``.
            logger.debug("[wow] Skipping done for timed-out turn %s", anchor)
            return
        character = normalize_character(chat_id) or str(chat_id)
        self._last_typing.pop(character, None)
        self._outbox(character).append("done", reply_to=anchor)
        logger.debug("[wow] done frame for %s (%s): %s", character, anchor, outcome)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        character = normalize_character(chat_id) or str(chat_id or "").strip()
        return {"name": character, "type": "dm", "chat_id": character}

    # ── modes (v1.1) ─────────────────────────────────────────────────────────

    def _source_character(self, source: Any) -> str:
        if source is None:
            return ""
        for attr in ("user_id", "chat_id"):
            value = getattr(source, attr, None)
            character = normalize_character(value)
            if character:
                return character
        return ""

    def toolsets_for_source(self, source: Any) -> list[str]:
        """Per-turn toolset override for *source* (research 07 §4.2).

        The gateway calls this before ``_get_platform_tools`` on every turn
        (``gateway/run_turn.py:434-457``), so a mode change applies to the next
        message with no restart and no new session.
        """
        character = self._source_character(source)
        try:
            state = self._modes.get(character)
        except Exception:
            logger.exception("[wow] mode lookup failed for %r; using the default", character)
            return list(modes.MODE_TOOLSETS[modes.DEFAULT_MODE])
        if state.expired and character not in self._expiry_handles:
            # The timer was lost (adapter restart / mode set while down): tell the
            # player once, here, instead of silently changing the toolset.
            self._outbox(character).append(
                "notice", text="Full control expired — back to standard mode.")
        return state.toolset_names

    def mode_snapshot(self) -> dict[str, Any]:
        return self._modes.snapshot()

    def _schedule_mode_expiry(self, character: str, expires_at: float) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        previous = self._expiry_handles.pop(character, None)
        if previous is not None:
            previous.cancel()
        delay = max(0.0, float(expires_at) - time.time())
        try:
            self._expiry_handles[character] = loop.call_later(
                delay, self._mode_expired, character, float(expires_at))
        except RuntimeError:  # pragma: no cover - loop closed between check and call
            self._expiry_handles.pop(character, None)

    def _reschedule_expiries(self) -> None:
        """Arm the expiry timer for every character already in a full window."""
        try:
            expiries = self._modes.full_expiries()
        except Exception:
            return
        for character, expires_at in expiries.items():
            self._schedule_mode_expiry(character, expires_at)

    def _mode_expired(self, character: str, expires_at: float) -> None:
        self._expiry_handles.pop(character, None)
        try:
            state = self._modes.get(character)  # downgrades + persists an elapsed window
        except Exception:
            logger.exception("[wow] could not expire the full window for %s", character)
            return
        if state.expired:
            minutes = int(self._modes.full_ttl_seconds // 60)
            self._outbox(character).append(
                "notice", text=f"Full control time-box ended ({minutes} min) — back to standard mode.")
        elif state.mode == "full" and state.expires_at and state.expires_at > time.time():
            self._schedule_mode_expiry(character, state.expires_at)

    async def handle_mode_command(self, raw_args: str = "", *, source: Any = None,
                                  platform: Any = None, **_: Any) -> str:
        """``/mode`` slash command (registered via ``ctx.register_command``)."""
        character = self._source_character(source)
        if not character:
            return "Modes are per character — this command only works from inside WoW."
        command = modes.parse(raw_args)
        if command.action == modes.ACTION_STATUS:
            return modes.status_line(self._modes.get(character))
        if command.action == modes.ACTION_SET and command.mode:
            self._modes.set(character, command.mode)
            if command.mode == "wow":
                return ("Mode: wow — WoW-only tools (addon search/install/uninstall, quest help) "
                        "plus web. Applies to your next message.")
            return ("Mode: standard — the usual assistant tools, no shell or MCP. "
                    "Applies to your next message.")
        if command.action == modes.ACTION_CHALLENGE:
            self._modes.begin_full(character)
            window = int(self._modes.confirm_window_seconds // 60)
            minutes = int(self._modes.full_ttl_seconds // 60)
            return (f"Full control gives Hermes terminal/file/code tools on your PC for {minutes} min. "
                    f"Reply `/mode full confirm` within {window} min to enable, or anything else to cancel.")
        if command.action == modes.ACTION_CONFIRM:
            state = self._modes.confirm_full(character)
            if state is None:
                window = int(self._modes.confirm_window_seconds // 60)
                return (f"No pending full-control request (it expires after {window} min). "
                        "Start again with `/mode full`.")
            self._schedule_mode_expiry(character, state.expires_at or time.time())
            until = datetime.fromtimestamp(state.expires_at or time.time(), tz=UTC).astimezone()
            minutes = int(self._modes.full_ttl_seconds // 60)
            logger.warning("[wow] full control enabled for %s until %s", character, until.isoformat())
            return (f"Full control ON until {until.strftime('%H:%M')} ({minutes} min). "
                    "Reply `/mode standard` to stop early.")
        # Unknown argument: answer with usage, keeping the current mode untouched.
        prefix = f"{command.error} " if command.error else ""
        return f"{prefix}{modes.USAGE}"


# ── plugin registration ─────────────────────────────────────────────────────


async def mode_command(raw_args: str = "", *, source: Any = None, platform: Any = None,
                       **kwargs: Any) -> str:
    """``/mode`` handler registered with ``ctx.register_command``.

    Declares ``source``/``platform`` so ``invoke_plugin_command`` passes this
    turn's chat context (``hermes_cli/plugin_command_dispatch.py``).
    """
    adapter = active_adapter()
    if adapter is None:
        return ("The WoW platform is not connected in this profile, so its mode cannot be "
                "changed. Start it with `hermes -p <profile> gateway start` and try again.")
    return await adapter.handle_mode_command(raw_args, source=source, platform=platform)



def check_requirements() -> bool:
    """PASSIVE dependency probe — importable, never installs."""
    return AIOHTTP_AVAILABLE


def validate_config(config: Any) -> bool:
    extra = getattr(config, "extra", {}) or {}
    host = str(_extra_or_secret(extra, "host", HOST_ENV, DEFAULT_HOST) or "").strip()
    port = coerce_port(_extra_or_secret(extra, "port", PORT_ENV, DEFAULT_PORT), DEFAULT_PORT)
    return bool(host) and 0 <= port <= 65535


def is_connected(config: Any) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(getattr(config, "enabled", False)) or bool(
        _extra_or_secret(extra, "bridge_token", BRIDGE_TOKEN_ENV, ""))


def _env_enablement() -> Optional[dict]:
    """``env_enablement_fn``: seed ``extra`` from the profile's env before adapter
    construction so env-only installs show in ``hermes gateway status``."""
    token = str(_get_scoped_secret(BRIDGE_TOKEN_ENV, "") or "").strip()
    if not token:
        return None
    seed = _seed_extra_from_env((
        (HOST_ENV, "host", None),
        (PORT_ENV, "port", int),
        (BRIDGE_TOKEN_ENV, "bridge_token", None),
    ), home_env=HOME_CHANNEL_ENV)
    return seed


async def _standalone_send(
    pconfig, chat_id: str, message: str, *,
    thread_id: Optional[str] = None, media_files: Optional[list[str]] = None,
    force_document: bool = False,
) -> dict[str, Any]:
    """Out-of-process cron delivery — not possible for this platform.

    The outbox lives in the live adapter's memory (the bridge long-polls it over
    HTTP), so a cron job running in a separate process has nothing to write to.
    ``deliver=wow`` therefore requires a co-resident gateway adapter; this stub
    returns the standard error envelope with a clear reason instead of pretending.
    """
    return send_error(
        "wow: the game outbox is served by the live gateway adapter; "
        "cron delivery needs a running gateway (no standalone sender exists)")


def _entry_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop ``PlatformEntry`` fields this core does not know (older Hermes)."""
    from gateway.platform_registry import PlatformEntry

    known = {f.name for f in dataclasses.fields(PlatformEntry)}
    return {key: value for key, value in kwargs.items() if key in known}


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    optional = _entry_kwargs({
        "is_connected": is_connected,
        "env_enablement_fn": _env_enablement,
        "cron_deliver_env_var": HOME_CHANNEL_ENV,
        "standalone_sender_fn": _standalone_send,
        "allowed_users_env": ALLOWED_USERS_ENV,
        "allow_all_env": ALLOW_ALL_ENV,
        "allowlist_suppresses_pairing": True,
        "max_message_length": MAX_MESSAGE_LENGTH,
        "platform_hint": PLATFORM_HINT,
        "pii_safe": True,
        "allow_update_command": False,
        "trusted_inbound": False,
        "emoji": "⚔️",
        "display_tier": "low",
        "dashboard_label": "World of Warcraft",
        "dashboard_description": (
            "Chat with Hermes from inside World of Warcraft through the companion bridge "
            "(local HTTP endpoint + long-poll outbox, bearer token)."),
    })
    ctx.register_platform(
        name="wow",
        label="World of Warcraft",
        adapter_factory=WoWAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=[BRIDGE_TOKEN_ENV],
        install_hint="No extra packages — aiohttp ships with Hermes",
        **optional,
    )
