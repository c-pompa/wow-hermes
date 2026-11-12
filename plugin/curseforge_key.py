"""One CurseForge API key per *machine*, with per-profile env fallback.

The key is not a per-character secret: every profile on this Mac (``default``,
``wow``, ``gamedev-s2`` …) belongs to the same player and the same CurseForge
account, so storing it once avoids re-entering it for every profile.

Storage
-------
``<hermes root>/plugin-data/wow/curseforge.key`` — a single line, mode ``0600``,
written atomically (temp file + ``fsync`` + rename) by
:func:`utils.atomic_write_text`. ``<hermes root>`` is :func:`machine_home`:
``get_hermes_home()`` without a ``profiles/<name>`` tail (default
``~/.hermes``), so the *default* profile and every named profile resolve to the
same file. On Windows the root is ``%LOCALAPPDATA%\\hermes``; profiles under it
share it too. Nothing is synced between machines — each machine has its own key.

Resolution order
----------------
1. the shared file (authoritative when it exists and is non-empty);
2. this profile's ``WOW_CURSEFORGE_API_KEY`` from its scoped ``.env`` (kept for
   installs that predate the shared file — never another profile's value);
3. none.

A shared file that exists but cannot be read falls back to the profile env with
a non-secret warning (path + error type only). The key itself is never returned
to a model, logged, or written into an HTTP response.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Tuple

CURSEFORGE_KEY_ENV = "WOW_CURSEFORGE_API_KEY"
SHARED_DIR_PARTS = ("plugin-data", "wow")
SHARED_FILE_NAME = "curseforge.key"
SECRET_MODE = 0o600
DIR_MODE = 0o700


def _warn(message: str) -> None:
    """Non-secret diagnostic on stderr; never includes file contents."""
    try:
        sys.stderr.write(f"[wow] {message}\n")
        sys.stderr.flush()
    except Exception:
        pass


def machine_home() -> Path:
    """The Hermes root shared by every profile on this machine.

    ``get_hermes_home()`` is the *active* profile directory when a scope is
    installed (setup/status run scoped). ``.../profiles/<name>`` therefore maps
    to ``...``; a root home maps to itself.
    """
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home())
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def shared_key_path() -> Path:
    """Absolute path of the machine-wide key file."""
    root = machine_home()
    return root.joinpath(*SHARED_DIR_PARTS, SHARED_FILE_NAME)


def _single_line(text: str) -> str:
    for line in str(text or "").splitlines():
        value = line.strip()
        if value:
            return value
    return ""


def read_shared_key(path: Optional[Path] = None) -> str:
    """The shared key, or ``""``. Never raises and never logs the value."""
    target = Path(path) if path is not None else shared_key_path()
    try:
        if not target.is_file():
            return ""
        try:
            mode = target.stat().st_mode & 0o777
            if mode & 0o077:  # group/other readable — tighten it, then read
                os.chmod(target, SECRET_MODE)
                _warn(f"tightened permissions on {target} to 0600")
        except OSError:
            pass  # permission repair is best-effort; readability decides
        return _single_line(target.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        _warn(f"shared CurseForge key at {target} is unreadable "
              f"({type(exc).__name__}); falling back to the profile env")
        return ""


def read_profile_key() -> str:
    """This profile's ``WOW_CURSEFORGE_API_KEY`` from its scoped secret env."""
    try:
        from gateway.platforms._shared import get_scoped_secret

        return _single_line(str(get_scoped_secret(CURSEFORGE_KEY_ENV, "") or ""))
    except Exception:
        return ""


def resolve_key() -> Tuple[str, Optional[str]]:
    """``(key, scope)`` with ``scope`` ``"shared"``, ``"profile"`` or ``None``."""
    shared = read_shared_key()
    if shared:
        return shared, "shared"
    profile = read_profile_key()
    if profile:
        return profile, "profile"
    return "", None


def key_status() -> dict:
    """``{"set": bool, "scope": "shared"|"profile"|None}`` — no key material."""
    key, scope = resolve_key()
    return {"set": bool(key), "scope": scope}


def save_shared_key(value: str) -> bool:
    """Write the shared key with ``0600`` (atomic). ``True`` when it changed.

    An identical existing value is left untouched, so a repeated Save is a
    no-op. The caller validates the token shape (single line, bounded length).
    """
    key = _single_line(value)
    if not key:
        raise ValueError("the CurseForge key must not be empty")
    target = shared_key_path()
    if read_shared_key(target) == key:
        return False
    from utils import atomic_write_text

    directory = target.parent
    directory.mkdir(parents=True, exist_ok=True)
    try:
        if (directory.stat().st_mode & 0o777) != DIR_MODE:
            os.chmod(directory, DIR_MODE)
    except OSError:
        pass
    atomic_write_text(target, key + "\n", mode=SECRET_MODE, create_mode=SECRET_MODE,
                      fsync_dir=True)
    try:
        os.chmod(target, SECRET_MODE)  # rename keeps the temp file's bits; be explicit
    except OSError:
        pass
    return True


def clear_shared_key() -> bool:
    """Remove the shared file (and tighten nothing else). ``True`` if removed."""
    target = shared_key_path()
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        _warn(f"could not remove the shared CurseForge key at {target} "
              f"({type(exc).__name__})")
        raise
