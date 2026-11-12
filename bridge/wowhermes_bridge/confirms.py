"""In-game player confirmation for bridge commands (PROTOCOL C + A.1 ``c`` records).

A command carrying ``confirm: "player"`` is **only proposed**. The bridge parks it here,
publishes it to the addon (slot key ``confirm``), and executes nothing until the addon
answers with ``c``: ``id=<command id>;ok=1``. Rules enforced here:

* same character and the **same session token** as the proposal;
* the command id must match exactly;
* at most 10 minutes old — after that the entry expires, the player gets a ``not confirmed,
  nothing was installed`` notice, and nothing was written;
* one use only: ``take`` removes the entry *before* the action runs, so a replayed ``c``
  can never execute anything twice.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

__all__ = ["CONFIRM_TTL", "PendingConfirm", "ConfirmStore", "TakeResult"]

#: research 08 §8.2: a proposal is valid for 10 minutes, no more.
CONFIRM_TTL = 600.0


@dataclass
class PendingConfirm:
    character: str
    session: str
    command_id: str
    action: str
    label: str
    frame: dict
    created: float
    expires: float

    def to_slot(self) -> dict:
        """The dict the addon reads out of the published slot (see docs/notes/ws-install.md)."""
        return {
            "id": self.command_id,
            "action": self.action,
            "name": self.label[:120],
            "t": int(self.created),
            "exp": int(self.expires),
        }


@dataclass(frozen=True)
class TakeResult:
    pending: PendingConfirm | None
    reason: str  # ok | unknown | expired | session_mismatch


class ConfirmStore:
    """One pending proposal per character; the newest command replaces an older one."""

    def __init__(self, *, ttl: float = CONFIRM_TTL, clock: Callable[[], float] = time.time):
        self.ttl = ttl
        self.clock = clock
        self._pending: dict[str, PendingConfirm] = {}

    def request(
        self,
        *,
        character: str,
        session: str,
        command_id: str,
        action: str,
        label: str,
        frame: dict,
    ) -> PendingConfirm:
        now = self.clock()
        pending = PendingConfirm(
            character=character,
            session=session,
            command_id=command_id,
            action=action,
            label=label,
            frame=dict(frame),
            created=now,
            expires=now + self.ttl,
        )
        self._pending[character] = pending
        return pending

    def newest(self, character: str, *, now: float | None = None) -> PendingConfirm | None:
        pending = self._pending.get(character)
        if pending is None:
            return None
        moment = self.clock() if now is None else now
        if moment >= pending.expires:
            return None
        return pending

    def take(
        self,
        character: str,
        session: str,
        command_id: str,
        *,
        now: float | None = None,
    ) -> TakeResult:
        moment = self.clock() if now is None else now
        pending = self._pending.get(character)
        if pending is None:
            return TakeResult(None, "unknown")
        if pending.command_id != command_id:
            return TakeResult(None, "unknown")
        if pending.session != session:
            # Strict: the proposal is bound to the session token it was made in. The addon
            # sends a hello (and the bridge records its session) before any `c` record.
            return TakeResult(None, "session_mismatch")
        # Consume before returning: a replay finds nothing, so nothing can run twice.
        self._pending.pop(character, None)
        if moment >= pending.expires:
            return TakeResult(None, "expired")
        return TakeResult(pending, "ok")

    def expire(self, *, now: float | None = None) -> list[PendingConfirm]:
        moment = self.clock() if now is None else now
        gone = [p for p in self._pending.values() if moment >= p.expires]
        for pending in gone:
            self._pending.pop(pending.character, None)
        return gone

    def clear(self, character: str | None = None) -> None:
        if character is None:
            self._pending.clear()
        else:
            self._pending.pop(character, None)

    def __len__(self) -> int:
        return len(self._pending)
