"""Per-character mode model for the ``wow`` platform plugin (v1.1).

Three modes, one per character (``Name-Realm``):

* ``standard`` — the hardened toolset the profile ships today (web/memory/skills/
  todo/cron/vision/image_gen/x_search/session_search/clarify, no shell, no MCP).
* ``wow`` — WoW-only: our own ``wow`` toolset plus read-only ``web``.
* ``full`` — ``hermes-cli`` (terminal/file/code). Two-step confirmation
  (``/mode full`` → ``/mode full confirm`` within 5 minutes) and time-boxed
  (60 minutes by default), then it lapses back to ``standard``.

Nothing in this module imports the gateway: it is the pure state/parsing half, so
it is unit-testable without a running adapter, and ``adapter.py`` can load it by
path in the same way the repo's tests load the adapter.

State lives in ``<hermes home>/plugin-data/wow/modes.json`` (mode ``0o600``)::

    {"characters": {"Name-Realm": {"mode": "full", "since": 1762700000.0,
                                   "expires_at": 1762703600.0}},
     "pending_full": {"Name-Realm": 1762700300.0}}

``pending_full`` holds the *deadline* of a confirmation challenge (5 min).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_MODE = "standard"

#: The hardened list from docs/STATUS.md (2026-11-08). Keep in sync with the
#: write in ``dashboard/plugin_api.py`` / ``backend_setup.py``.
STANDARD_TOOLSETS: list[str] = [
    "clarify", "cronjob", "image_gen", "memory", "session_search",
    "skills", "todo", "vision", "web", "x_search", "no_mcp", "wow",
]
WOW_TOOLSETS: list[str] = ["wow", "web", "no_mcp"]
FULL_TOOLSETS: list[str] = ["hermes-cli"]

MODE_TOOLSETS: dict[str, list[str]] = {
    "standard": STANDARD_TOOLSETS,
    "wow": WOW_TOOLSETS,
    "full": FULL_TOOLSETS,
}
MODES: tuple[str, ...] = ("standard", "wow", "full")

FULL_TTL_SECONDS = 60 * 60        # time-box for full control
CONFIRM_WINDOW_SECONDS = 5 * 60   # /mode full → /mode full confirm
MAX_STATE_BYTES = 64 * 1024

MODE_ALIASES = {
    "standard": "standard", "normal": "standard", "default": "standard", "safe": "standard",
    "wow": "wow", "wow-only": "wow", "wowonly": "wow", "game": "wow",
    "full": "full", "control": "full", "all": "full",
}

USAGE = (
    "Modes: standard (normal tools), wow (WoW-only tools + web), "
    "full (your whole toolset, 60 min, needs confirmation). "
    "Use /mode standard|wow|full."
)

# ── character key ───────────────────────────────────────────────────────────
# ``Name-Realm``. Character names are 2-12 letters; realms may carry digits,
# apostrophes and hyphens. A realm with spaces is folded to hyphens once, here.
_CHARACTER_RE = re.compile(r"^[^\W\d_]{2,12}-[\w'\-]{2,40}$", re.UNICODE)


def normalize_character(raw: Any) -> str:
    """Canonical ``Name-Realm`` key, or ``""`` when the value is unusable."""
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value or len(value) > 64:
        return ""
    name, sep, realm = value.partition("-")
    if not sep:
        return ""
    realm = re.sub(r"\s+", "-", realm.strip()).strip("-")
    canonical = f"{name.strip()}-{realm}"
    return canonical if _CHARACTER_RE.match(canonical) else ""


# ── paths ───────────────────────────────────────────────────────────────────


def default_data_dir() -> Path:
    """``<hermes home>/plugin-data/wow`` (profile-scoped, matching ``ctx.state``)."""
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()) / "plugin-data" / "wow"
    except Exception:  # pragma: no cover - hermes_constants always imports in the gateway
        return Path(os.path.expanduser("~/.hermes")) / "plugin-data" / "wow"


def _plugin_settings() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        entry = ((cfg.get("plugins") or {}).get("entries") or {}).get("wow") or {}
        settings = entry.get("settings") or {}
        return settings if isinstance(settings, dict) else {}
    except Exception:
        return {}


def resolve_default_mode() -> str:
    """``plugins.entries.wow.settings.default_mode`` from the profile config.

    Read through the profile-scoped config helper so a multiplexed process never
    picks up another profile's settings. Only ``standard`` and ``wow`` are legal
    defaults: ``full`` always needs the in-game two-step confirmation, so it is
    never reached by default — even if a hand-edited config says so. Any failure
    (no config, unreadable plugin entry) falls back to ``standard``.
    """
    mode = str(_plugin_settings().get("default_mode") or "").strip().lower()
    return mode if mode in {"standard", "wow"} else DEFAULT_MODE


def resolve_full_ttl_seconds() -> float:
    """``plugins.entries.wow.settings.full_ttl_minutes`` (default 60 min), clamped."""
    try:
        minutes = float(_plugin_settings().get("full_ttl_minutes") or 0)
    except (TypeError, ValueError):
        return float(FULL_TTL_SECONDS)
    if not 1 <= minutes <= 24 * 60:
        return float(FULL_TTL_SECONDS)
    return minutes * 60.0


# ── state ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModeState:
    """Effective mode for one character and why."""

    mode: str
    since: float = 0.0
    expires_at: Optional[float] = None
    expired: bool = False  # a full window just lapsed (or had lapsed) on read

    @property
    def toolset_names(self) -> list[str]:
        return list(MODE_TOOLSETS.get(self.mode, MODE_TOOLSETS[DEFAULT_MODE]))

    def remaining_seconds(self, now: Optional[float] = None) -> Optional[float]:
        if self.expires_at is None:
            return None
        return max(0.0, self.expires_at - (time.time() if now is None else now))


class ModeStore:
    """JSON-backed per-character mode store (atomic writes, ``0o600``).

    ``default_mode_getter`` is called on each read that needs a default, so a
    settings change applies without recreating the store.
    """

    def __init__(
        self,
        data_dir: Optional[Path | str] = None,
        *,
        default_mode_getter: Optional[Callable[[], str]] = None,
        full_ttl_seconds: float = FULL_TTL_SECONDS,
        confirm_window_seconds: float = CONFIRM_WINDOW_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
        self.path = self.data_dir / "modes.json"
        self._default_mode_getter = default_mode_getter or resolve_default_mode
        self.full_ttl_seconds = float(full_ttl_seconds)
        self.confirm_window_seconds = float(confirm_window_seconds)
        self._clock = clock

    # -- io ---------------------------------------------------------------
    def _read(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            # Corrupt state must never take the adapter down; start clean.
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
        if len(encoded.encode("utf-8")) > MAX_STATE_BYTES:
            raise ValueError("wow mode state exceeds its size cap")
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    # -- defaults ---------------------------------------------------------
    def default_mode(self) -> str:
        try:
            mode = str(self._default_mode_getter() or "").strip().lower()
        except Exception:
            return DEFAULT_MODE
        return mode if mode in MODE_TOOLSETS else DEFAULT_MODE

    # -- reads ------------------------------------------------------------
    def get(self, character: str) -> ModeState:
        """Effective state, downgrading an elapsed ``full`` window on read."""
        key = normalize_character(character) or str(character or "")
        if not key:
            return ModeState(self.default_mode())
        data = self._read()
        record = (data.get("characters") or {}).get(key)
        if not isinstance(record, dict):
            return ModeState(self.default_mode())
        mode = str(record.get("mode") or "").strip().lower()
        if mode not in MODE_TOOLSETS:
            return ModeState(self.default_mode())
        expires_at = record.get("expires_at")
        try:
            expires_at = float(expires_at) if expires_at is not None else None
        except (TypeError, ValueError):
            expires_at = None
        if mode == "full" and expires_at is not None and self._clock() >= expires_at:
            self._downgrade(key, data)
            return ModeState(self.default_mode(), expired=True)
        return ModeState(mode, float(record.get("since") or 0.0), expires_at)

    def mode_for(self, character: str) -> str:
        return self.get(character).mode

    def toolsets_for(self, character: str) -> list[str]:
        return self.get(character).toolset_names

    def snapshot(self) -> dict[str, Any]:
        """Every stored character -> effective mode, for the status endpoint."""
        data = self._read()
        out: dict[str, str] = {}
        for key in sorted((data.get("characters") or {})):
            out[key] = self.get(key).mode
        return {"default_mode": self.default_mode(), "characters": out,
                "pending_full": sorted((data.get("pending_full") or {}))}

    def full_expiries(self) -> dict[str, float]:
        """``{character: expires_at}`` for every character inside a live full window."""
        data = self._read()
        out: dict[str, float] = {}
        for key, record in (data.get("characters") or {}).items():
            if not isinstance(record, dict) or str(record.get("mode")) != "full":
                continue
            raw = record.get("expires_at")
            if raw is None:
                continue
            try:
                expires_at = float(raw)
            except (TypeError, ValueError):
                continue
            if expires_at > self._clock():
                out[str(key)] = expires_at
        return out

    # -- writes -----------------------------------------------------------
    def set(self, character: str, mode: str, *, expires_at: Optional[float] = None,
            now: Optional[float] = None) -> ModeState:
        key = normalize_character(character) or str(character or "").strip()
        if not key:
            raise ValueError("invalid character")
        mode = str(mode or "").strip().lower()
        if mode not in MODE_TOOLSETS:
            raise ValueError(f"unknown mode {mode!r}")
        now = self._clock() if now is None else now
        data = self._read()
        characters = data.setdefault("characters", {})
        characters[key] = {"mode": mode, "since": now, "expires_at": expires_at}
        pending = data.get("pending_full")
        if isinstance(pending, dict):
            pending.pop(key, None)
            if not pending:
                data.pop("pending_full", None)
        self._write(data)
        return ModeState(mode, now, expires_at)

    def clear(self, character: str) -> ModeState:
        return self.set(character, self.default_mode())

    # -- full-control confirmation ---------------------------------------
    def begin_full(self, character: str, *, now: Optional[float] = None) -> float:
        """Record a confirmation challenge; returns its deadline (epoch)."""
        key = normalize_character(character) or str(character or "").strip()
        if not key:
            raise ValueError("invalid character")
        now = self._clock() if now is None else now
        deadline = now + self.confirm_window_seconds
        data = self._read()
        data.setdefault("pending_full", {})[key] = deadline
        self._write(data)
        return deadline

    def pending_full(self, character: str, *, now: Optional[float] = None) -> Optional[float]:
        """Deadline of a live challenge, or ``None`` (expired challenges are pruned)."""
        key = normalize_character(character) or str(character or "").strip()
        now = self._clock() if now is None else now
        data = self._read()
        pending = data.get("pending_full")
        if not isinstance(pending, dict):
            return None
        try:
            deadline = float(pending.get(key) or 0.0)
        except (TypeError, ValueError):
            return None
        if deadline <= now:
            if key in pending:
                pending.pop(key, None)
                if not pending:
                    data.pop("pending_full", None)
                self._write(data)
            return None
        return deadline

    def confirm_full(self, character: str, *, now: Optional[float] = None) -> Optional[ModeState]:
        """Consume a live challenge and switch to full for one TTL window."""
        now = self._clock() if now is None else now
        deadline = self.pending_full(character, now=now)
        if deadline is None:
            return None
        return self.set(character, "full", expires_at=now + self.full_ttl_seconds, now=now)

    def _downgrade(self, key: str, data: dict[str, Any]) -> None:
        characters = data.setdefault("characters", {})
        characters[key] = {
            "mode": self.default_mode(),
            "since": self._clock(),
            "expires_at": None,
        }
        try:
            self._write(data)
        except Exception:
            pass  # best effort: the in-memory answer is already the safe one


# ── command parsing ─────────────────────────────────────────────────────────

#: ``parse`` action names.
ACTION_STATUS = "status"
ACTION_SET = "set"
ACTION_CHALLENGE = "challenge"
ACTION_CONFIRM = "confirm"
ACTION_HELP = "help"


@dataclass(frozen=True)
class ModeCommand:
    action: str
    mode: Optional[str] = None
    error: str = ""


def parse(raw_args: Any) -> ModeCommand:
    """Parse the tail of ``/mode`` into an action.

    ``""``/``status`` → status; ``standard|wow`` → set; ``full`` → challenge;
    ``full confirm`` → confirm. Anything else → help with ``error`` set.
    """
    text = str(raw_args or "").strip().lower()
    tokens = [t for t in re.split(r"\s+", text) if t]
    if not tokens or tokens[0] in {"status", "?"}:
        return ModeCommand(ACTION_STATUS)
    if tokens == ["help"]:
        return ModeCommand(ACTION_HELP)
    if len(tokens) == 1:
        word = tokens[0]
        if word == "confirm":
            return ModeCommand(ACTION_HELP, error="Say what to confirm: /mode full confirm")
        mode = MODE_ALIASES.get(word)
        if mode == "full":
            return ModeCommand(ACTION_CHALLENGE, mode="full")
        if mode:
            return ModeCommand(ACTION_SET, mode=mode)
        return ModeCommand(ACTION_HELP, error=f"Unknown mode {word!r}.")
    if tokens == ["full", "confirm"]:
        return ModeCommand(ACTION_CONFIRM, mode="full")
    return ModeCommand(ACTION_HELP, error=f"Unknown arguments {' '.join(tokens)!r}.")


def status_line(state: ModeState, *, now: Optional[float] = None) -> str:
    if state.mode == "full":
        remaining = state.remaining_seconds(now)
        minutes = int((remaining or 0) // 60)
        return (f"Mode: full control — {minutes} min left (expires automatically). "
                "Modes: standard, wow, full.")
    return f"Mode: {state.mode}. Modes: standard, wow, full. {USAGE}"


def describe(mode: str) -> str:
    if mode == "wow":
        return ("Mode: wow — WoW-only tools (addon search/install/uninstall, quest help) plus web. "
                "Takes effect on your next message.")
    if mode == "full":
        return ("Mode: full control — your whole toolset, including terminal/file/code. "
                f"Time-boxed to {int(FULL_TTL_SECONDS // 60)} min.")
    return ("Mode: standard — the usual assistant tools, no shell or MCP. "
            "Takes effect on your next message.")
