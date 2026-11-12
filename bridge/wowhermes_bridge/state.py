"""Per-character exchange state and the persisted transcript.

The overlay's transcript lives here (``%LOCALAPPDATA%\\wowhermes\\transcripts.json``), together
with the outbox cursor and the highest acked outbound id. Nothing in this file talks to the
game or the network; it is the bridge's memory.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .slots import (
    MAX_REPLY_BYTES,
    MAX_SLOT_REPLIES,
    RESTORE_ENTRY_BYTES,
    atomic_write_bytes,
    clip_utf8,
    truncate_reply,
)

TRANSCRIPT_VERSION = 1
MAX_HANDLED = 4096


@dataclass
class Exchange:
    """One outbound message and its (eventual) reply."""

    character: str
    chat: str
    id: int
    session: str
    text: str
    created: float
    updated: float
    status: str = "working"  # working | done | error
    reply: str = ""
    error: str | None = None
    synthetic: bool = False
    seen_by_addon: bool = False

    @property
    def key(self) -> str:
        return f"{self.session}:{self.id}"

    def to_dict(self) -> dict:
        return asdict(self)


class BridgeState:
    """Transcript + cursors, persisted as one JSON document."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else None
        self.data: dict = {"version": TRANSCRIPT_VERSION, "characters": {}}
        self.exchanges: dict[str, Exchange] = {}
        self.dirty = False

    # -- persistence -------------------------------------------------------
    def load(self) -> "BridgeState":
        if self.path and self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict) and "characters" in loaded:
                self.data = loaded
                for raw in (loaded.get("exchanges") or {}).values():
                    try:
                        exchange = Exchange(**raw)
                    except TypeError:
                        continue
                    self.exchanges[exchange.key] = exchange
        return self

    def save(self) -> None:
        if not self.path:
            return
        self.data["exchanges"] = {k: e.to_dict() for k, e in self.exchanges.items()}
        payload = json.dumps(self.data, ensure_ascii=False, indent=1).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(self.path, payload)
        self.dirty = False

    # -- buckets -----------------------------------------------------------
    def bucket(self, character: str) -> dict:
        characters = self.data.setdefault("characters", {})
        return characters.setdefault(
            character,
            {
                "session": "",
                "since": 0,
                "ack": 0,
                "last_id": 0,
                "messages": [],
                "handled": [],
                "synthetic": 0,
                "context": {},
                "quest": "",
                "hello": {},
                "next_slot": None,
                "next_slot_session": "",
                "slots_installed": 0,
                "sound_mode": "0",
                "last_seen": 0.0,
            },
        )

    def characters(self) -> list[str]:
        return list(self.data.get("characters", {}))

    # -- cursors -----------------------------------------------------------
    def since(self, character: str) -> int:
        return int(self.bucket(character).get("since", 0))

    def set_since(self, character: str, value: int) -> None:
        bucket = self.bucket(character)
        if int(value) != bucket.get("since"):
            bucket["since"] = int(value)
            self.dirty = True

    def ack(self, character: str) -> int:
        return int(self.bucket(character).get("ack", 0))

    def set_ack(self, character: str, value: int, *, force: bool = False) -> None:
        bucket = self.bucket(character)
        if force or int(value) > int(bucket.get("ack", 0)):
            bucket["ack"] = int(value)
            self.dirty = True

    def session(self, character: str) -> str:
        return str(self.bucket(character).get("session", ""))

    def set_session(self, character: str, session: str) -> bool:
        """Returns True when the session token is new (SavedVariables wiped or re-minted)."""
        bucket = self.bucket(character)
        previous = bucket.get("session") or ""
        bucket["session"] = session
        if previous != session:
            self.dirty = True
            return True
        return False

    def context(self, character: str) -> dict:
        return dict(self.bucket(character).get("context") or {})

    def set_context(self, character: str, context: dict) -> None:
        bucket = self.bucket(character)
        if context != bucket.get("context"):
            bucket["context"] = dict(context)
            self.dirty = True

    def quest(self, character: str) -> str:
        """The latest ``q`` snapshot text (PROTOCOL A.1 v1.1), "" when none was seen."""
        return str(self.bucket(character).get("quest") or "")

    def set_quest(self, character: str, text: str) -> None:
        bucket = self.bucket(character)
        if text != bucket.get("quest"):
            bucket["quest"] = str(text)
            self.dirty = True

    def hello(self, character: str) -> dict:
        return dict(self.bucket(character).get("hello") or {})

    def next_slot(self, character: str) -> int | None:
        """The latest ``q`` flag (PROTOCOL A.1): the slot the addon will load next.

        ``None`` means no ``q`` was ever seen (an addon without the flag); ``0`` means
        the addon reported its slot pool exhausted.
        """
        value = self.bucket(character).get("next_slot")
        return None if value is None else int(value)

    def note_next_slot(self, character: str, session: str, q: int) -> None:
        """Remember the newest ``q`` for the session that sent it (B.2 priority window)."""
        bucket = self.bucket(character)
        if int(q) != bucket.get("next_slot") or session != bucket.get("next_slot_session"):
            bucket["next_slot"] = int(q)
            bucket["next_slot_session"] = str(session)
            self.dirty = True

    def reset_next_slot(self, character: str) -> None:
        bucket = self.bucket(character)
        bucket["next_slot"] = None
        bucket["next_slot_session"] = ""

    def note_hello(self, character: str, fields: dict) -> None:
        bucket = self.bucket(character)
        bucket["hello"] = {**dict(bucket.get("hello") or {}), **fields}
        bucket["slots_installed"] = int(fields.get("p") or bucket.get("slots_installed") or 0)
        bucket["sound_mode"] = str(fields.get("s") or bucket.get("sound_mode") or "0")
        bucket["last_seen"] = time.time()
        self.dirty = True

    # -- dedupe ------------------------------------------------------------
    def is_handled(self, character: str, session: str, frame_id: int) -> bool:
        return f"{session}:{frame_id}" in self.bucket(character).get("handled", [])

    def mark_handled(self, character: str, session: str, frame_id: int) -> None:
        bucket = self.bucket(character)
        handled = bucket.setdefault("handled", [])
        key = f"{session}:{frame_id}"
        if key not in handled:
            handled.append(key)
            if len(handled) > MAX_HANDLED:
                del handled[: len(handled) - MAX_HANDLED]
            self.dirty = True

    # -- transcript --------------------------------------------------------
    def add_message(self, character: str, role: str, *, id: int, text: str, t: float, status: str = "done") -> None:
        bucket = self.bucket(character)
        messages = bucket.setdefault("messages", [])
        entry = {"role": role, "id": int(id), "t": float(t), "text": text, "status": status}
        # A reply replaces the placeholder row for the same id instead of stacking up.
        for index, existing in enumerate(messages):
            if existing.get("role") == role and int(existing.get("id", -1)) == int(id):
                messages[index] = entry
                self.dirty = True
                return
        messages.append(entry)
        del messages[:-500]
        self.dirty = True

    def messages(self, character: str) -> list[dict]:
        return list(self.bucket(character).get("messages") or [])

    # -- exchanges ---------------------------------------------------------
    def put_exchange(self, exchange: Exchange) -> None:
        self.exchanges[exchange.key] = exchange
        self.dirty = True

    def get_exchange(self, key: str) -> Exchange | None:
        return self.exchanges.get(key)

    def new_synthetic(self, character: str, text: str, *, t: float | None = None) -> Exchange:
        """An unsolicited agent message (cron/send_message) becomes its own exchange."""
        bucket = self.bucket(character)
        bucket["synthetic"] = int(bucket.get("synthetic", 0)) + 1
        now = time.time() if t is None else t
        exchange = Exchange(
            character=character,
            chat="c0",
            id=0,
            session=f"~{bucket['synthetic']}",
            text=text,
            created=now,
            updated=now,
            status="done",
            reply=text,
            synthetic=True,
        )
        self.put_exchange(exchange)
        return exchange

    def pending_replies(self, character: str) -> list[Exchange]:
        return [
            e
            for e in self.exchanges.values()
            if e.character == character and e.status in ("working", "done", "error")
        ]

    # -- slot state --------------------------------------------------------
    def recent_replies(self, character: str, limit: int = MAX_SLOT_REPLIES) -> list[dict]:
        """The latest state per exchange, newest last (PROTOCOL B.2 caps applied here)."""
        items: list[dict] = []
        for exchange in sorted(self.exchanges.values(), key=lambda e: (e.updated, e.id)):
            if exchange.character != character:
                continue
            if exchange.synthetic:
                continue
            text = exchange.reply or ("…" if exchange.status == "working" else "")
            if exchange.status == "error":
                text = exchange.error or "error"
            items.append(
                {
                    "chat": exchange.chat,
                    "id": exchange.id,
                    "status": exchange.status,
                    "t": int(exchange.updated),
                    "text": truncate_reply(text),
                }
            )
        return items[-limit:]

    def restore_bundle(
        self,
        character: str,
        *,
        max_entries: int = 64,
        max_bytes: int = RESTORE_ENTRY_BYTES,
    ) -> list[dict] | None:
        """PROTOCOL B.2: history for a brand-new session token, newest entries first.

        Each entry carries at most ``max_bytes`` UTF-8 text bytes; the 10 KB bundle budget
        is enforced when the slot is rendered (``slots.fit_slot_state``).
        """
        messages = self.messages(character)
        if not messages:
            return None
        trimmed: list[dict] = []
        for entry in reversed(messages[-max_entries:]):
            text = clip_utf8(str(entry.get("text", "")), max_bytes)[0]
            trimmed.append(
                {
                    "role": str(entry.get("role", "hermes")),
                    "id": int(entry.get("id", 0)),
                    "t": int(float(entry.get("t", 0))),
                    "text": text,
                }
            )
        return trimmed


def build_slot_state(
    state: BridgeState,
    character: str,
    *,
    bridge: str = "up",
    now: float | None = None,
    restore: list[dict] | None = None,
    confirm: dict | None = None,
    note: str | None = None,
    reload: dict | None = None,
    boot: float | None = None,
) -> dict:
    moment = time.time() if now is None else now
    slot = {
        "v": 1,
        "now": int(moment),
        "ts": float(moment),
        "ack": state.ack(character),
        "bridge": bridge,
        "char": character,
        "replies": state.recent_replies(character),
        **({"restore": restore} if restore else {}),
    }
    # Bridge -> addon additions used by the confirmed-command flow (WS-H). Both are optional:
    # an older addon ignores unknown keys, and an older bridge never sends them.
    if confirm:
        slot["confirm"] = confirm
    if note:
        slot["note"] = note[:400]
    # v1.3: post-install manual /reload reminder. The bridge republishes it for
    # ~60 s so a slow slot load still sees it.
    if reload:
        slot["reload"] = reload
    # Publisher incarnation: seq restarts with the bridge process; the addon uses
    # this to distinguish a real restart from an older slot read out of order.
    if boot is not None:
        slot["boot"] = float(boot)
    return slot

