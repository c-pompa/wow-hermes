"""Backend implementation for the ``wow`` package: ``POST /setup`` and ``GET /status``.

Runs inside the gateway process (``dashboard/plugin_api.py`` is a thin FastAPI
wrapper around these functions). Research 07 §2/§3.5/§6 is the contract:

* **Idempotent.** A second Save with the same inputs changes nothing, and never
  rotates a working bridge token unless ``rotate_token`` is explicitly true.
* **Never logs the token.** It may be *returned* exactly once (first creation or
  an explicit rotation) so the player can pair the bridge.
* **Profile-scoped writes.** Every ``config.yaml`` / ``.env`` read or write goes
  through ``set_hermes_home_override(profile_dir)`` — the same helper the REST
  profile routes use — never the launch profile's ambient home.
* **Migration.** An existing profile listing ``wow-platform`` in
  ``plugins.enabled`` is rewritten to ``wow`` (the id rename in v1.1).

Hard rules baked in here:

* ``WOW_ALLOW_ALL_USERS`` is removed (allowlist is ``WOW_ALLOWED_USERS=<character>``).
* The CurseForge key is a **profile-scoped Hermes secret**
  (``WOW_CURSEFORGE_API_KEY`` in the selected profile's ``.env``, written while
  that profile's home override is installed). There is no plugin-managed key
  file; the shared-file design of v0.2.0 is retired and its key is NOT read.
  Saving is idempotent; blank leaves the value unchanged and ``clear`` removes
  it from the selected profile only.
* ``known_plugin_toolsets.wow = ["wow"]`` is written so the WoW toolset is *off*
  unless a mode names it (research 07 risk 3 — plugin toolsets default ON).
* A hand-edited ``SOUL.md`` is never overwritten (``<!-- wow:managed:sha=… -->``),
  and the managed ``wow-player`` skill follows the same marker rule while
  describing **every** allowlisted character (v1.3 F2).
* Research sources (v1.3) are a per-profile developer setting stored in
  ``plugin-data/wow/sources.json`` — see ``sources.py`` for why they are not in
  ``config.yaml`` (the adapter reads them on every turn, in another process).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from . import modes, sources
from .tools import CURSEFORGE_KEY_ENV

PKG_ROOT = Path(__file__).resolve().parent
PACKAGE_ID = "wow"
LEGACY_ID = "wow-platform"
DEFAULT_PROFILE = "wow"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8791
WAIT_SECONDS = 40.0
COPY_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", ".pytest_cache", "tests", "*.pyc", ".DS_Store")

SOUL_MARKER_RE = re.compile(r"<!--\s*wow:managed(?::sha=([0-9a-f]{64}))?[^>]*-->")
_SOUL_MARKER_LINE = re.compile(r"\n?<!--\s*wow:managed[^>]*-->\s*$")


class SetupError(ValueError):
    """Bad request data (the API layer maps it to HTTP 400)."""


#: Research-source validation error, re-exported so the API layer has one import.
SourceError = sources.SourceError


# ── profile helpers ─────────────────────────────────────────────────────────


def _profiles():
    from hermes_cli import profiles

    return profiles


def _home_scope(profile_dir: Path):
    """Enter a profile-scoped config/env/secret scope: returns ``(token, reset)``.

    The home override routes Hermes's config helpers. The explicit secret scope
    is equally important: without it, ``get_secret`` still sees the gateway's
    launch profile (or ambient process env), not the profile being configured.
    Routed-profile semantics make a miss fail closed instead of borrowing that
    other profile's value.
    """
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from agent.secret_scope import (
        build_profile_secret_scope,
        reset_multiplex_context,
        reset_secret_scope,
        set_multiplex_context,
        set_secret_scope,
    )

    token = set_hermes_home_override(profile_dir)
    try:
        secret_token = set_secret_scope(
            build_profile_secret_scope(profile_dir), profile_home=str(profile_dir))
        multiplex_token = set_multiplex_context(True)
    except Exception:
        reset_hermes_home_override(token)
        raise

    def reset() -> None:
        reset_multiplex_context(multiplex_token)
        reset_secret_scope(secret_token)
        reset_hermes_home_override(token)

    return token, reset


def _canonical_profile(name: Any) -> str:
    profiles = _profiles()
    raw = str(name or DEFAULT_PROFILE).strip().lower()
    if not raw:
        raw = DEFAULT_PROFILE
    try:
        profiles.validate_profile_name(raw)
    except ValueError as exc:
        raise SetupError(str(exc)) from None
    if raw == "default":
        raise SetupError("the WoW profile cannot be 'default'")
    return raw


def _character(raw_any: Any, realm_any: Any = "") -> str:
    # Novice-friendly: the addon's character key uses hyphens for spaces in both
    # character and realm names ("Mr Pomps" -> "Player-Realm"). Accept the display
    # spelling here instead of making the player know that transport detail.
    raw = re.sub(r"\s+", "-", str(raw_any or "").strip())
    realm = re.sub(r"\s+", "-", str(realm_any or "").strip())
    candidate = f"{raw}-{realm}" if realm and "-" not in raw else raw
    candidate = re.sub(r"-{2,}", "-", candidate)
    character = modes.normalize_character(candidate)
    if not character:
        raise SetupError(
            "character must be a full Name-Realm key (e.g. Player-Realm), "
            "or Name plus realm")
    return character


# ── string renders ──────────────────────────────────────────────────────────


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _soul_body(text: str) -> str:
    """The SOUL text with any managed marker stripped and exactly one trailing newline."""
    return _SOUL_MARKER_LINE.sub("", text or "").rstrip() + "\n"


def render_soul(character: str, profile: str = DEFAULT_PROFILE,
                template_path: Optional[Path] = None) -> str:
    """Render ``SOUL.md.tmpl`` for *character*, with a managed-marker suffix.

    The character key is substituted **verbatim**: ``Player-Realm`` is the key,
    not a name plus a realm called ``Eater`` (v1.3 F2 — splitting it produced a
    fake realm in SOUL and in the player skill).
    """
    path = template_path or (PKG_ROOT / "SOUL.md.tmpl")
    template = path.read_text(encoding="utf-8")
    rendered = (template.replace("{character}", character)
                        .replace("{profile}", profile))
    body = _soul_body(rendered)
    marker = f"<!-- wow:managed:sha={_digest(body)} profile={profile} character={character} -->"
    return f"{body}\n{marker}\n"


def soul_is_ours(existing: str) -> bool:
    """True when *existing* still carries our marker and nothing has edited it."""
    match = SOUL_MARKER_RE.search(existing or "")
    if not match or not match.group(1):
        return False
    return _digest(_soul_body(existing)) == match.group(1)


def _clean_characters(characters: Any) -> list[str]:
    """Normalised, de-duplicated character keys in the caller's order."""
    raw = characters if isinstance(characters, (list, tuple, set)) else [characters]
    out: list[str] = []
    for item in raw:
        key = modes.normalize_character(item) or ""
        if key and key not in out:
            out.append(key)
    return out


def render_profile_skill(characters: Any, profile: str = DEFAULT_PROFILE,
                         template_path: Optional[Path] = None) -> str:
    """Render the managed ``wow-player`` profile skill for *characters*.

    One section per allowlisted character, each showing the ``Name-Realm`` key
    verbatim (never split into name + realm). The body carries the same
    ``<!-- wow:managed:sha=… -->`` contract as SOUL: the setup only rewrites the
    file while that marker matches the content byte-for-byte.
    """
    path = template_path or (PKG_ROOT / "profile-skills" / "wow-player" / "SKILL.md")
    template = path.read_text(encoding="utf-8")
    keys = _clean_characters(characters)
    listing = "\n".join(f"- `{key}`" for key in keys)
    inline = ", ".join(f"`{key}`" for key in keys)
    rendered = (template.replace("{characters}", listing)
                        .replace("{characters_inline}", inline)
                        .replace("{profile}", profile))
    body = _soul_body(rendered)
    marker = (f"<!-- wow:managed:sha={_digest(body)} profile={profile} "
              f"characters={','.join(keys)} -->")
    return f"{body}\n{marker}\n"


def skill_is_ours(existing: str) -> bool:
    """True when the managed player skill still matches its own marker digest."""
    return soul_is_ours(existing)


def _merge_allowed(existing: Any, character: str) -> list[str]:
    """Existing allowlist keys plus *character* (order preserved, de-duplicated).

    The setup form can add a character but has no removal control, so writing
    just the submitted one would silently drop every other character of the
    profile — and the managed skill (v1.3 F2) is rendered from this union.
    """
    parts = [part.strip() for part in str(existing or "").split(",") if part.strip()]
    merged = _clean_characters(parts)
    if character and character not in merged:
        merged.append(character)
    return merged


# ── adapters/status ─────────────────────────────────────────────────────────


def _adapter_status() -> dict[str, Any]:
    """``status_snapshot()`` from the live adapter module in this process, if any."""
    for module in list(sys.modules.values()):
        path = str(getattr(module, "__file__", "") or "")
        if path.endswith("/wow/adapter.py") or path.endswith("\\wow\\adapter.py"):
            snapshot = getattr(module, "status_snapshot", None)
            if callable(snapshot):
                try:
                    return dict(snapshot() or {})
                except Exception:
                    return {}
    return {}


def _probe(host: str, port: int, timeout: float = 0.5) -> bool:
    if not host or not port:
        return False
    target = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    try:
        with socket.create_connection((target, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


HEALTH_TIMEOUT = 1.0
HEALTH_BODY_LIMIT = 65536
# Patched by tests so no unit test opens a socket.
_HTTP_OPEN = urllib.request.urlopen


def _health_url(host: str, port: int) -> Optional[str]:
    """``http://host:port/v1/wow/health`` for a configured endpoint, else ``None``."""
    raw = str(host or "").strip()
    if not raw or not port:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.\-:]+", raw):
        return None
    if raw in {"0.0.0.0", "::"}:
        raw = "127.0.0.1"
    elif ":" in raw and not raw.startswith("["):
        raw = f"[{raw}]"
    return f"http://{raw}:{int(port)}/v1/wow/health"


def _adapter_health(host: str, port: int, token: str = "",
                    timeout: float = HEALTH_TIMEOUT) -> dict[str, Any]:
    """Ask the configured adapter ``GET /v1/wow/health`` whether it is live.

    Returns ``{ok, connected, characters, error}``. ``ok`` means the endpoint
    answered a well-formed health body; ``connected`` is the bridge's own view
    (an addon is actually attached). The bearer token is sent only in the
    ``Authorization`` header and never appears in the result, an exception
    message, or a log line. The whole probe is bounded by *timeout* (default
    1s) so a dead endpoint cannot stall ``/status``.
    """
    url = _health_url(host, port)
    if url is None:
        return {"ok": False, "connected": False, "characters": [], "error": "no endpoint"}
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _HTTP_OPEN(request, timeout=timeout) as response:
            raw = response.read(HEALTH_BODY_LIMIT)
    except urllib.error.HTTPError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        reason = "unauthorized" if code == 401 else (f"http {code}" if code else "health failed")
        return {"ok": False, "connected": False, "characters": [], "error": reason}
    except (urllib.error.URLError, OSError, ValueError):
        return {"ok": False, "connected": False, "characters": [],
                "error": "unreachable"}
    except Exception:  # a broken adapter must never break /status
        return {"ok": False, "connected": False, "characters": [],
                "error": "health failed"}
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return {"ok": False, "connected": False, "characters": [],
                "error": "bad response"}
    if not isinstance(payload, dict) or payload.get("ok") is False:
        return {"ok": False, "connected": False, "characters": [],
                "error": "not ready"}
    characters = [str(item).strip() for item in (payload.get("characters") or [])
                  if str(item).strip()]
    return {"ok": True, "connected": bool(payload.get("connected")),
            "characters": characters, "error": None}


def _wait_listening(host: str, port: int, seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        if _probe(host, port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


# ── setup ───────────────────────────────────────────────────────────────────


def setup(request: dict[str, Any], *, pkg_root: Optional[Path] = None,
          wait_seconds: Optional[float] = None, notify: bool = True) -> dict[str, Any]:
    """View of one Save. Returns ``{ok, profile, character, created, steps, token_once?}``."""
    req = dict(request or {})
    character = _character(req.get("character"), req.get("realm"))
    profile_name = _canonical_profile(req.get("profile_name") or req.get("profile"))
    default_mode = str(req.get("default_mode") or modes.DEFAULT_MODE).strip().lower()
    if default_mode not in modes.MODE_TOOLSETS:
        raise SetupError(f"unknown default_mode {default_mode!r} (standard|wow|full)")
    if default_mode == "full":
        raise SetupError("default_mode 'full' is not allowed; full control always needs "
                         "an in-game confirmation")
    host = str(req.get("host") or DEFAULT_HOST).strip() or DEFAULT_HOST
    if not re.fullmatch(r"[A-Za-z0-9_.\-:]+", host):
        raise SetupError("host must be an IP address or hostname")
    try:
        port = int(req.get("port") if req.get("port") is not None else DEFAULT_PORT)
    except (TypeError, ValueError):
        raise SetupError("port must be an integer") from None
    if not 1 <= port <= 65535:
        raise SetupError("port must be 1..65535")
    rotate = bool(req.get("rotate_token"))
    # ``curseforge_api_key``: "" (or absent) = leave unchanged, "clear" = remove,
    # anything else = the player's own key. It is never returned or logged.
    cf_request = req.get("curseforge_api_key")
    cf_action = "unchanged"
    cf_key: Optional[str] = None
    if cf_request is not None:
        cf_value = str(cf_request).strip()
        if cf_value == "clear":
            cf_action = "cleared"
        elif cf_value:
            if len(cf_value) > 300 or re.search(r"\s", cf_value):
                raise SetupError("curseforge_api_key must be a single token (no whitespace, "
                                 "max 300 chars); send \"clear\" to remove it")
            cf_key = cf_value
            cf_action = "saved"
    root = Path(pkg_root) if pkg_root is not None else PKG_ROOT
    wait = WAIT_SECONDS if wait_seconds is None else float(wait_seconds)
    if not (root / "plugin.yaml").is_file():
        raise SetupError(f"{root} is not the wow plugin package (plugin.yaml missing)")

    profiles = _profiles()
    target = profiles.get_profile_dir(profile_name)
    steps: list[dict[str, Any]] = []

    def step(name: str, fn):
        try:
            detail = fn()
            steps.append({"step": name, "ok": True, "detail": detail})
            return detail
        except Exception as exc:  # report, never abort half-way
            steps.append({"step": name, "ok": False, "detail": str(exc)[:400]})
            return None

    created = not target.exists()

    def do_profile():
        if target.exists():
            return "exists"
        profiles.create_profile(
            profile_name, description=f"In-game assistant for {character}.")
        return "created"

    step("profile", do_profile)
    if not target.exists():
        return {"ok": False, "profile": profile_name, "character": character,
                "created": False, "steps": steps,
                "error": "profile creation failed"}

    def do_package():
        dest = target / "plugins" / PACKAGE_ID
        shutil.copytree(root, dest, dirs_exist_ok=True, ignore=COPY_IGNORE)
        return str(dest)

    step("package", do_package)

    token_once: Optional[str] = None
    token_created = False

    # The profile's allowlist after this Save: every character this profile
    # answers for, which the managed player skill must describe (v1.3 F2).
    allowed_out: list[str] = []

    _, reset_scope = _home_scope(target)
    try:
        def do_env():
            nonlocal token_once, token_created, allowed_out
            from hermes_cli.config import remove_env_value, save_env_value
            from gateway.platforms._shared import get_scoped_secret

            existing = str(get_scoped_secret("WOW_BRIDGE_TOKEN", "") or "").strip()
            if rotate or not existing:
                import secrets

                token = secrets.token_hex(32)
                token_created = not existing and not rotate
                token_once = token
                save_env_value("WOW_BRIDGE_TOKEN", token)
                action = "rotated" if rotate else "generated"
            else:
                token = existing
                action = "kept"
            save_env_value("WOW_HOST", host)
            save_env_value("WOW_PORT", str(port))
            # Union, not overwrite: this form can add a character but cannot
            # express removal, and dropping a second allowlisted character would
            # lock it out of its own profile.
            allowed = _merge_allowed(
                str(get_scoped_secret("WOW_ALLOWED_USERS", "") or ""), character)
            allowed_out = allowed
            save_env_value("WOW_ALLOWED_USERS", ",".join(allowed))
            # Keep an existing home channel (it may be the other character).
            home = modes.normalize_character(
                str(get_scoped_secret("WOW_HOME_CHANNEL", "") or "").strip())
            save_env_value("WOW_HOME_CHANNEL", home or character)
            remove_env_value("WOW_ALLOW_ALL_USERS")
            return action

        def do_config():
            from hermes_cli.config import load_config, save_config

            cfg = load_config() or {}
            plugins = cfg.setdefault("plugins", {})
            enabled = plugins.get("enabled")
            if not isinstance(enabled, list):
                enabled = []
            migrated = [PACKAGE_ID if str(item) == LEGACY_ID else str(item) for item in enabled]
            if PACKAGE_ID not in migrated:
                migrated.append(PACKAGE_ID)
            plugins["enabled"] = list(dict.fromkeys(migrated))
            entry = plugins.setdefault("entries", {}).setdefault(PACKAGE_ID, {})
            settings = entry.setdefault("settings", {})
            settings["default_mode"] = default_mode
            settings["host"] = host
            settings["port"] = port
            cfg.setdefault("platform_toolsets", {})[PACKAGE_ID] = list(modes.STANDARD_TOOLSETS)
            cfg.setdefault("known_plugin_toolsets", {})[PACKAGE_ID] = [PACKAGE_ID]
            # The WoW toolset is intentionally small. Keep its plugin tools eager:
            # default deferral hid wow_addon_uninstall behind tool_search and the
            # model incorrectly claimed uninstall was unsupported.
            cfg.setdefault("tools", {}).setdefault("tool_search", {})["defer"] = []
            gw = cfg.setdefault("gateway", {}).setdefault("platforms", {}).setdefault(PACKAGE_ID, {})
            gw["enabled"] = True
            disp = cfg.setdefault("display", {}).setdefault("platforms", {}).setdefault(PACKAGE_ID, {})
            disp.update({"show_reasoning": False, "tool_progress": "off",
                         "interim_assistant_messages": False})
            save_config(cfg, strip_defaults=False)
            renamed = LEGACY_ID in [str(item) for item in enabled]
            return "renamed wow-platform" if renamed else ("enabled" if created else "updated")

        def do_soul():
            from utils import atomic_write_text

            path = target / "SOUL.md"
            existing = ""
            if path.exists():
                try:
                    existing = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    existing = ""
                # A profile we just created carries the generic bootstrap SOUL —
                # that is not a user edit. Any other pre-existing SOUL is.
                if existing.strip() and not soul_is_ours(existing) and not created:
                    return "kept (hand-edited)"
            rendered = render_soul(character, profile_name)
            if rendered == existing:
                return "unchanged"
            atomic_write_text(path, rendered, preserve_mode=True, create_mode=0o644)
            return "written"

        def do_skills():
            dest_root = target / "skills"
            dest_root.mkdir(parents=True, exist_ok=True)
            template = root / "profile-skills" / "wow-player" / "SKILL.md"
            if not template.is_file():
                return "no template"
            dest = dest_root / "wow-player"
            dest.mkdir(parents=True, exist_ok=True)
            path = dest / "SKILL.md"
            # Every allowlisted character, not just the one being saved (F2).
            characters = allowed_out or [character]
            rendered = render_profile_skill(characters, profile_name,
                                            template_path=template)
            existing = ""
            if path.exists():
                existing = path.read_text(encoding="utf-8", errors="ignore")
            if existing == rendered:
                return "unchanged"
            # Same rule as SOUL: only refresh a copy we still own byte-for-byte;
            # a player-edited skill is kept as-is.
            if existing.strip() and not skill_is_ours(existing):
                return "kept (edited)"
            path.write_text(rendered, encoding="utf-8")
            return "written" if not existing.strip() else "updated"

        step("env", do_env)

        def do_curseforge_key():
            if cf_action == "unchanged":
                return "unchanged"
            from hermes_cli.config import remove_env_value, save_env_value
            from gateway.platforms._shared import get_scoped_secret

            if cf_action == "cleared":
                # Selected profile only: the home override above routes every
                # read/write through Hermes's own config helper.
                remove_env_value(CURSEFORGE_KEY_ENV)
                return "cleared (profile)"
            existing = str(get_scoped_secret(CURSEFORGE_KEY_ENV, "") or "").strip()
            if existing == cf_key:
                return "unchanged (profile)"
            save_env_value(CURSEFORGE_KEY_ENV, str(cf_key))
            return "saved (profile)"

        step("curseforge_key", do_curseforge_key)
        step("config", do_config)
        step("soul", do_soul)
        step("skills", do_skills)

        # Report which scope now supplies the key (never the key itself). Only the
        # selected profile is consulted.
        try:
            from agent.secret_scope import build_profile_secret_scope

            # Rebuild after Save/Clear: the scope installed at function entry is
            # intentionally an immutable snapshot of the pre-write profile.
            profile_now = str(build_profile_secret_scope(target).get(
                CURSEFORGE_KEY_ENV, "") or "").strip()
        except Exception:
            profile_now = ""
        cf_scope = "profile" if profile_now else None
    finally:
        reset_scope()

    served: Optional[list[str]] = None

    def do_serve():
        nonlocal served
        if not notify:
            return "skipped"
        from hermes_cli.gateway_multiplex_served import notify_multiplexer_profiles_changed

        served = notify_multiplexer_profiles_changed(profile_name)
        return {"hot_served": served is not None, "profiles": served}

    step("serve", do_serve)

    listening = False
    if wait > 0:
        listening = _wait_listening(host, port, wait)
    steps.append({"step": "listener", "ok": listening,
                  "detail": f"{host}:{port} {'listening' if listening else 'not listening yet'}"})

    result: dict[str, Any] = {
        "ok": all(item["ok"] for item in steps if item["step"] not in {"listener"}),
        "profile": profile_name,
        "character": character,
        "characters": allowed_out or [character],
        "created": created,
        "host": host,
        "port": port,
        "default_mode": default_mode,
        "listening": listening,
        "curseforge_key": cf_action,   # "saved" | "cleared" | "unchanged" — never the value
        "curseforge_key_scope": cf_scope,  # "profile" | None (no shared scope)
        "steps": steps,
    }
    if token_once:
        # Only on first creation or an explicit rotation — for pairing, once.
        result["token_once"] = token_once
        result["token_created"] = token_created
    if served:
        result["served_profiles"] = served
    result["restart_required"] = not listening
    if not listening:
        # Config written from the backend is not live in memory (research 07 risk 5):
        # say so instead of claiming the bridge is up.
        result["note"] = ("Not listening yet. If the gateway was already running, start/restart "
                          "this profile's gateway: `hermes -p %s gateway start`." % profile_name)
    return result


# ── status ──────────────────────────────────────────────────────────────────


def status(*, profile_name: str = DEFAULT_PROFILE, character: str = "") -> dict[str, Any]:
    """Read-only snapshot for ``GET /api/plugins/wow/status``."""
    profile_name = _canonical_profile(profile_name)
    profiles = _profiles()
    target = profiles.get_profile_dir(profile_name)
    out: dict[str, Any] = {
        "ok": True, "profile": profile_name, "profile_exists": target.exists(),
        "plugin_enabled": False, "legacy_plugin_enabled": False,
        "platform_enabled": False, "platform_toolsets": None,
        "known_plugin_toolsets": None, "default_mode": modes.DEFAULT_MODE,
        "mode": None, "modes": {}, "token_set": False, "listening": False,
        "connected": False, "listener": False,
        "last_bridge_hello": None, "last_hello": None, "last_seen": None,
        "sources": sources.DEFAULTS.as_dict(),
    }
    if not target.exists():
        out["error"] = "profile does not exist yet — save the setup form first"
        return out

    _, reset_scope = _home_scope(target)
    try:
        from hermes_cli.config import load_config
        from gateway.platforms._shared import get_scoped_secret

        cfg = load_config() or {}
        # Read configuration and secrets through the selected profile's Hermes
        # scope. Never parse its .env directly or borrow ambient process values.
        env = {name: str(get_scoped_secret(name, "") or "") for name in (
            "WOW_HOST", "WOW_PORT", "WOW_ALLOWED_USERS", "WOW_ALLOW_ALL_USERS",
            "WOW_HOME_CHANNEL", "WOW_BRIDGE_TOKEN", CURSEFORGE_KEY_ENV,
        )}
    except Exception as exc:
        out["error"] = f"cannot read the profile: {str(exc)[:200]}"
        return out
    finally:
        reset_scope()

    # Profile-scoped only: the queried profile's own secret store. No shared
    # file, and no other profile is ever consulted. No key material leaves here.
    profile_env_key = str(env.get(CURSEFORGE_KEY_ENV) or "").strip()
    cf_scope = "profile" if profile_env_key else None

    plugins = cfg.get("plugins") or {}
    enabled = [str(item) for item in (plugins.get("enabled") or [])]
    settings = (((plugins.get("entries") or {}).get(PACKAGE_ID) or {}).get("settings") or {})
    out["plugin_enabled"] = PACKAGE_ID in enabled
    out["legacy_plugin_enabled"] = LEGACY_ID in enabled
    out["platform_enabled"] = bool((((cfg.get("gateway") or {}).get("platforms") or {})
                                    .get(PACKAGE_ID) or {}).get("enabled"))
    out["platform_toolsets"] = (cfg.get("platform_toolsets") or {}).get(PACKAGE_ID)
    out["known_plugin_toolsets"] = (cfg.get("known_plugin_toolsets") or {}).get(PACKAGE_ID)
    out["default_mode"] = str(settings.get("default_mode") or modes.DEFAULT_MODE)
    host = str(env.get("WOW_HOST") or settings.get("host") or DEFAULT_HOST)
    try:
        port = int(env.get("WOW_PORT") or settings.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    out["host"], out["port"] = host, port
    out["token_set"] = bool(str(env.get("WOW_BRIDGE_TOKEN") or "").strip())
    out["allowed_users"] = str(env.get("WOW_ALLOWED_USERS") or "")
    out["allow_all_users"] = str(env.get("WOW_ALLOW_ALL_USERS") or "").strip().lower() in {
        "1", "true", "yes", "on"}
    out["home_channel"] = str(env.get("WOW_HOME_CHANNEL") or "")
    out["curseforge_key_set"] = bool(profile_env_key)
    out["curseforge_key_scope"] = cf_scope

    # Store + live adapter. The store is read from the target profile's data dir,
    # independent of which profile's gateway hosts this route.
    store_dir = target / "plugin-data" / "wow"
    # Research sources (v1.3): same per-profile data dir as modes.json, read with
    # a lenient fallback so a hand-edited file can never break /status.
    out["sources"] = sources.SourceStore(store_dir).load().as_dict()
    configured_default = str(settings.get("default_mode") or modes.DEFAULT_MODE)
    if configured_default not in {"standard", "wow"}:
        configured_default = modes.DEFAULT_MODE
    store = modes.ModeStore(store_dir, default_mode_getter=lambda: configured_default)
    try:
        snapshot = store.snapshot()
        out["modes"] = snapshot.get("characters") or {}
        out["default_mode"] = configured_default
    except Exception:
        pass
    wanted = character or out.get("allowed_users", "").split(",")[0].strip() or ""
    if wanted:
        try:
            out["mode"] = store.get(wanted).mode
        except Exception:
            out["mode"] = None
    out["character"] = wanted or None

    snapshot = _adapter_status()
    token = str(env.get("WOW_BRIDGE_TOKEN") or "").strip()
    health = _adapter_health(host, port, token)
    reachable = bool(health.get("ok"))
    # TCP fallback only when /health did not answer: a listener that refuses the
    # bearer still owns the port, which is a different fact from "bridge online".
    tcp_listening = True if reachable else _probe(host, port)
    out["connected"] = bool(health.get("connected"))
    out["listener"] = bool(reachable or tcp_listening or snapshot.get("listening"))
    out["listening"] = out["listener"]
    out["adapter"] = snapshot or None
    out["bound"] = snapshot.get("bound") or (f"{host}:{port}" if out["listener"] else "")
    out["last_bridge_hello"] = snapshot.get("last_bridge_hello")
    out["last_hello"] = out["last_bridge_hello"]
    out["last_seen"] = snapshot.get("last_seen")
    if health.get("error"):
        out["health_error"] = health["error"]
    if not out.get("character") and len(health.get("characters") or []) == 1:
        # One bridge character and no allowlist answer: show it rather than "—".
        out["character"] = health["characters"][0]
    if snapshot.get("token_set"):
        out["token_set"] = True
    return out


def set_sources(request: dict[str, Any]) -> dict[str, Any]:
    """Save the per-profile research sources (``POST /api/plugins/wow/sources``).

    Body: ``{profile, primary?, secondary?, other?, reset?}``. Idempotent — the
    same values twice leave the file untouched (``changed: false``). A missing or
    blank field keeps the stored value; ``reset: true`` restores the defaults and
    ignores the values. Every value is validated; a bad one raises
    :class:`sources.SourceError` (the API maps it to 400) instead of being
    sanitized, because these strings are interpolated into the agent's prompt.

    The write target is the selected profile's ``plugin-data/wow/sources.json``
    (resolved from ``get_profile_dir``), so the adapter in that profile's gateway
    picks it up on its next turn without a restart. No secret material is read,
    written or returned.
    """
    req = dict(request or {})
    profile_name = _canonical_profile(req.get("profile") or req.get("profile_name"))
    reset = bool(req.get("reset"))
    profiles = _profiles()
    target = profiles.get_profile_dir(profile_name)
    if not target.exists():
        raise SetupError(f"profile {profile_name!r} does not exist yet — save the setup form first")
    store = sources.SourceStore(target / "plugin-data" / "wow")
    if reset:
        wanted = sources.DEFAULTS
    else:
        current = store.load()
        primary = str(req.get("primary") or "").strip()
        secondary = str(req.get("secondary") or "").strip()
        other = str(req.get("other") or "").strip()
        wanted = sources.Sources(
            primary=sources.normalize_host(primary, "primary") if primary else current.primary,
            secondary=(sources.normalize_host(secondary, "secondary") if secondary
                       else current.secondary),
            other=sources.normalize_other(other) if other else current.other,
        )
    changed = store.save(wanted)
    return {
        "ok": True,
        "profile": profile_name,
        "sources": wanted.as_dict(),
        "defaults": sources.DEFAULTS.as_dict(),
        "changed": bool(changed),
        "reset": reset,
    }


def migrate_enabled(enabled: Any) -> list[str]:
    """``plugins.enabled`` with the legacy id renamed to ``wow`` (order preserved)."""
    items = [str(item) for item in (enabled or [])]
    return list(dict.fromkeys(PACKAGE_ID if item == LEGACY_ID else item for item in items))
