"""Per-profile research-source setting for the ``wow`` platform plugin (v1.3).

The player (or the developer configuring the game profile) picks where in-game
answers may look things up:

* ``primary``   — searched first (default ``wowhead.com``).
* ``secondary`` — searched when the primary has nothing (default ``ahledger.com``).
* ``other``     — ``web`` (a general web search, the default), ``none`` (no other
  search), or up to three hostnames.

State lives in ``<hermes home>/plugin-data/wow/sources.json`` (mode ``0o600``)::

    {"primary": "wowhead.com", "secondary": "ahledger.com", "other": "web"}

Why a JSON file and not the profile's ``config.yaml``: the adapter reads this
value on *every inbound turn* inside the multiplexed gateway, which hosts several
profiles. A small file in the same profile-scoped directory as ``modes.json``
gives the adapter one ``stat()`` per turn and an mtime-checked cache, needs no
gateway restart and no config reload, and cannot pick up another profile's
values as long as the path comes from ``get_hermes_home()`` (which is
profile-scoped in a multiplexed host, exactly like ``ModeStore``). Settings that
belong to *setup* (host/port/default mode) stay in ``config.yaml``.

Nothing in this module imports the gateway: it is the pure validation/state
half, so the adapter can load it by path (``_load_sibling``) exactly like
``modes.py``.

Prompt-injection hygiene: the values are interpolated into the agent's prompt
line, so they are restricted to bare hostnames (optionally a path prefix) or
the two literals ``web``/``none``. No scheme, no spaces, no quotes, no
newlines, <=100 chars. A rejected value is a 400, never a sanitized guess.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

DEFAULT_PRIMARY = "wowhead.com"
DEFAULT_SECONDARY = "ahledger.com"
DEFAULT_OTHER = "web"

MAX_FIELD_CHARS = 100
MAX_OTHER_HOSTS = 3
OTHER_SPECIALS: tuple[str, ...] = ("web", "none")
MAX_STATE_BYTES = 8 * 1024

# DNS label, then a dotted name with at least one dot. Hosts only: no scheme,
# no port, no userinfo, no trailing dot, no whitespace.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST = rf"{_LABEL}(?:\.{_LABEL})+"
# Optional path prefix: /segment/segment (each segment URL-safe, non-empty).
_PATH = r"(?:/[A-Za-z0-9._~%+-]+)*"
_HOST_PATH_RE = re.compile(rf"^{_HOST}{_PATH}$")
_PLAIN_HOST_RE = re.compile(rf"^{_HOST}$")


class SourceError(ValueError):
    """A research-source value that must be rejected (the API maps it to 400)."""


# ── validation ──────────────────────────────────────────────────────────────


def normalize_host(raw: Any, field: str = "source") -> str:
    """A bare hostname, optionally with a path prefix; raises :class:`SourceError`."""
    if not isinstance(raw, str):
        raise SourceError(f"{field} must be a hostname string (e.g. {DEFAULT_PRIMARY})")
    value = raw.strip().lower()
    if not value:
        raise SourceError(f"{field} cannot be empty")
    if len(value) > MAX_FIELD_CHARS:
        raise SourceError(f"{field} must be at most {MAX_FIELD_CHARS} characters")
    if not _HOST_PATH_RE.match(value):
        raise SourceError(
            f"{field} must be a bare hostname with an optional path, e.g. {DEFAULT_PRIMARY} "
            f"or {DEFAULT_PRIMARY}/forever — no scheme, spaces or quotes")
    return value


def normalize_other(raw: Any) -> str:
    """``web`` | ``none`` | up to three hostnames, canonicalised with ``, ``."""
    if not isinstance(raw, str):
        raise SourceError("other must be 'web', 'none' or a comma list of hostnames")
    value = raw.strip().lower()
    if not value:
        raise SourceError("other must be 'web', 'none' or a comma list of hostnames")
    if value in OTHER_SPECIALS:
        return value
    parts = [part.strip() for part in value.split(",")]
    hosts = [part for part in parts if part]
    if any(not part for part in parts):
        raise SourceError("other has an empty entry — remove the stray comma")
    if not 1 <= len(hosts) <= MAX_OTHER_HOSTS:
        raise SourceError(f"other accepts at most {MAX_OTHER_HOSTS} hostnames")
    for host in hosts:
        if host in OTHER_SPECIALS:
            raise SourceError("other cannot mix 'web'/'none' with hostnames")
        if len(host) > MAX_FIELD_CHARS:
            raise SourceError(f"each other hostname must be at most {MAX_FIELD_CHARS} characters")
        if not _PLAIN_HOST_RE.match(host):
            raise SourceError(
                f"other entry {host!r} must be a bare hostname (no scheme, path, port or quotes)")
    # De-duplicate while keeping the player's order.
    unique = list(dict.fromkeys(hosts))
    return ", ".join(unique)


# ── model ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Sources:
    """The three research sources of one Hermes profile."""

    primary: str = DEFAULT_PRIMARY
    secondary: str = DEFAULT_SECONDARY
    other: str = DEFAULT_OTHER

    def as_dict(self) -> dict[str, str]:
        return {"primary": self.primary, "secondary": self.secondary, "other": self.other}

    def context_line(self) -> str:
        """The one line the agent sees before the player's message."""
        return (f"[WoW sources: primary={self.primary}; secondary={self.secondary}; "
                f"other={self.other}]")

    def other_hosts(self) -> list[str]:
        """The hostnames behind ``other`` (``[]`` for ``web``/``none``)."""
        if self.other in OTHER_SPECIALS:
            return []
        return [host.strip() for host in self.other.split(",") if host.strip()]

    @classmethod
    def from_mapping(cls, data: Any) -> "Sources":
        """Tolerant read: an unusable field falls back to its default, never raises.

        A hand-edited ``sources.json`` must not be able to (a) crash a turn or
        (b) inject arbitrary text into the prompt line.
        """
        if not isinstance(data, Mapping):
            return cls()
        defaults = {"primary": DEFAULT_PRIMARY, "secondary": DEFAULT_SECONDARY}
        out: dict[str, str] = {}
        for field, fallback in defaults.items():
            try:
                out[field] = normalize_host(data.get(field), field)
            except SourceError:
                out[field] = fallback
        try:
            out["other"] = normalize_other(data.get("other"))
        except SourceError:
            out["other"] = DEFAULT_OTHER
        return cls(**out)


DEFAULTS = Sources()


# ── store ───────────────────────────────────────────────────────────────────

#: path -> ((mtime_ns, size), Sources). One entry per profile served by this
#: process; re-stat'ed on every turn, so a save in another process is picked up
#: on the next message without a gateway restart.
_CACHE: dict[str, tuple[tuple[int, int], Sources]] = {}


def default_data_dir() -> Path:
    """``<hermes home>/plugin-data/wow`` — profile-scoped under a multiplexed gateway."""
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()) / "plugin-data" / "wow"
    except Exception:  # pragma: no cover - hermes_constants always imports in the gateway
        return Path(os.path.expanduser("~/.hermes")) / "plugin-data" / "wow"


class SourceStore:
    """JSON-backed per-profile research sources (atomic writes, ``0o600``).

    ``data_dir=None`` resolves the profile home on every access (a fresh
    ``get_hermes_home()``, which follows the multiplexed profile scope) instead
    of freezing whatever home existed when the adapter was constructed.
    """

    def __init__(self, data_dir: Optional[Path | str] = None,
                 path: Optional[Path | str] = None) -> None:
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._path = Path(path) if path is not None else None

    @property
    def path(self) -> Path:
        if self._path is not None:
            return self._path
        base = self._data_dir if self._data_dir is not None else default_data_dir()
        return base / "sources.json"

    # -- reads -------------------------------------------------------------
    def load(self) -> Sources:
        """Current value; defaults when the file is absent, unreadable or invalid."""
        path = self.path
        key = str(path)
        try:
            stat = os.stat(path)
        except OSError:
            _CACHE.pop(key, None)
            return DEFAULTS
        stamp = (int(stat.st_mtime_ns), int(stat.st_size))
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            raw = path.read_text(encoding="utf-8-sig")
            data = json.loads(raw)
        except (OSError, ValueError, UnicodeDecodeError):
            return DEFAULTS
        value = Sources.from_mapping(data)
        _CACHE[key] = (stamp, value)
        return value

    # -- writes ------------------------------------------------------------
    def save(self, value: Sources) -> bool:
        """Persist *value*; returns True when the file actually changed."""
        if not isinstance(value, Sources):
            raise SourceError("sources must be a Sources value")
        # Re-validate even a constructed value: every write is a trust boundary.
        clean = Sources(primary=normalize_host(value.primary, "primary"),
                        secondary=normalize_host(value.secondary, "secondary"),
                        other=normalize_other(value.other))
        if self.load() == clean and self.path.exists():
            return False
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(clean.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        if len(encoded.encode("utf-8")) > MAX_STATE_BYTES:
            raise SourceError("sources state exceeds its size cap")
        tmp = path.with_name(f".{path.name}.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        stat = os.stat(path)
        _CACHE[str(path)] = ((int(stat.st_mtime_ns), int(stat.st_size)), clean)
        return True

    def reset(self) -> Sources:
        """Restore the defaults and write them back (explicit, not file deletion)."""
        self.save(DEFAULTS)
        return DEFAULTS


def clear_cache() -> None:
    """Drop the mtime cache — tests only; production re-stats on every turn."""
    _CACHE.clear()
