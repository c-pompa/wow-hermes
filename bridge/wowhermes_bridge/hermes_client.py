"""PROTOCOL C: the bridge's HTTP client for the Hermes ``wow`` platform plugin.

Two calls matter: ``POST /v1/wow/message`` (fire a player message at the agent) and
``GET /v1/wow/outbox`` (long-poll for replies). The outbox cursor is persisted by the
bridge *before* frames are acted on, so a crash at worst replays nothing (PROTOCOL C).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Callable

import httpx

DEFAULT_TIMEOUT = 15.0


class HermesClientError(RuntimeError):
    """Any non-2xx / transport failure talking to the gateway."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ConfigError(HermesClientError):
    """401/403: the bearer token is missing or wrong. A configuration error, not a hiccup.

    The app surfaces it once (overlay health + log) and must stop retrying hot — the fix
    is editing ``config.toml`` / ``WOWHERMES_TOKEN``, not waiting.
    """


#: Historical name; 401/403 are still the only thing raised as this family.
HermesUnauthorized = ConfigError


@dataclass
class OutboxBatch:
    character: str
    next: int
    messages: list[dict]
    #: The adapter restarted (its sequence space began at 1 again) and replayed from the
    #: start; ``next`` may be below the cursor we asked with.
    reset: bool = False


def resolve_outbox_cursor(current: int, batch: OutboxBatch) -> int:
    """The cursor to persist after *batch*: forward normally, rewind on a reset.

    A restarted gateway adapter replays its retained frames from seq 1 and flags
    ``reset``; the cursor must follow it down or the bridge polls a cursor that can never
    exist again (WS-N incident: bridge stuck at since=21 while the reply sat at seq 6).
    When frames are present the cursor may never land below the highest frame seq, so a
    replay is not replayed forever.
    """
    next_cursor = int(batch.next)
    if not batch.reset and next_cursor >= int(current):
        return next_cursor
    highest = max((int(frame.get("seq") or 0) for frame in batch.messages), default=0)
    return max(next_cursor, highest) if batch.messages else next_cursor


class HermesClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=self.base_url, timeout=timeout)

    # -- plumbing ----------------------------------------------------------
    @property
    def headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "wowhermes-bridge/0.1"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, path: str, *, timeout: float | None = None, **kwargs) -> httpx.Response:
        try:
            response = self._client.request(
                method, path, headers=self.headers, timeout=timeout or DEFAULT_TIMEOUT, **kwargs
            )
        except httpx.HTTPError as exc:
            raise HermesClientError(f"{method} {path} failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise ConfigError(f"{method} {path} unauthorized ({response.status_code})", response.status_code)
        if response.status_code >= 300:
            raise HermesClientError(
                f"{method} {path} returned {response.status_code}: {response.text[:200]}",
                response.status_code,
            )
        return response

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "HermesClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- endpoints ---------------------------------------------------------
    def health(self) -> dict:
        return self._request("GET", "/v1/wow/health").json()

    def post_message(
        self,
        *,
        character: str,
        chat: str,
        msg_id: str,
        text: str,
        ts: float,
        context: dict | None = None,
        client: dict | None = None,
    ) -> dict:
        body = {
            "character": character,
            "chat": chat,
            "msg_id": msg_id,
            "text": text,
            "ts": ts,
        }
        if context:
            body["context"] = {k: str(v) for k, v in context.items()}
        if client:
            body["client"] = client
        response = self._request("POST", "/v1/wow/message", json=body)
        try:
            return response.json()
        except json.JSONDecodeError:
            return {"ok": True}

    def get_outbox(self, character: str, since: int = 0, wait: float = 0) -> OutboxBatch:
        wait = max(0, min(float(wait), 25.0))
        timeout = max(DEFAULT_TIMEOUT, wait + 8.0)
        response = self._request(
            "GET",
            "/v1/wow/outbox",
            params={"character": character, "since": int(since), "wait": int(wait)},
            timeout=timeout,
        )
        data = response.json()
        return OutboxBatch(
            character=data.get("character", character),
            next=int(data.get("next", since)),
            messages=list(data.get("messages") or []),
            reset=bool(data.get("reset")),
        )


class OutboxPoller(threading.Thread):
    """Long-polls one character's outbox and hands frames to ``on_frames``.

    The cursor is advanced (and persisted through ``on_cursor``) *before* the frames are
    delivered, per PROTOCOL C's backpressure rule.
    """

    def __init__(
        self,
        client: HermesClient,
        character: str,
        since: int,
        on_frames: Callable[[list[dict]], None],
        *,
        wait: float = 20.0,
        on_cursor: Callable[[int], None] | None = None,
        stop_event: threading.Event | None = None,
        min_backoff: float = 1.0,
        max_backoff: float = 30.0,
        on_error: Callable[[BaseException], None] | None = None,
        logger: Callable[[str], None] | None = None,
    ):
        super().__init__(name=f"outbox-{character}", daemon=True)
        self.client = client
        self.character = character
        self.since = int(since)
        self.on_frames = on_frames
        self.wait = wait
        self.on_cursor = on_cursor
        self.stop_event = stop_event or threading.Event()
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff
        self.on_error = on_error
        self._logger = logger or (lambda message: None)
        self.last_error: str | None = None
        self.stopped_for_config = False

    def run(self) -> None:  # pragma: no cover - exercised via threads in tests
        backoff = self.min_backoff
        while not self.stop_event.is_set():
            try:
                batch = self.client.get_outbox(self.character, since=self.since, wait=self.wait)
            except ConfigError as exc:
                # A wrong token cannot fix itself: stop this poller instead of retrying hot.
                self.last_error = str(exc)
                self.stopped_for_config = True
                self._logger(f"outbox unauthorized (configuration error): {exc}")
                if self.on_error:
                    self.on_error(exc)
                return
            except Exception as exc:  # noqa: BLE001 - the poll loop must never die
                self.last_error = str(exc)
                self._logger(f"outbox poll failed: {exc}")
                if self.on_error:
                    self.on_error(exc)
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, self.max_backoff)
                continue

            backoff = self.min_backoff
            self.last_error = None
            cursor = resolve_outbox_cursor(self.since, batch)
            self.since = cursor
            if self.on_cursor:
                self.on_cursor(cursor)
            if batch.messages:
                self.on_frames(batch.messages)
            elif self.wait <= 0:
                time.sleep(0.5)
