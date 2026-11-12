"""WoW-only tools (v1.1): addon discovery, install guidance, uninstall, quest help.

Registered under ``toolset="wow"`` by ``__init__.py`` so the ``wow`` mode can
offer exactly these tools (plus read-only ``web``). Design constraints from
``docs/research/08-wow-only-mode.md`` and PLAN-v1.1 item 5:

* **No bundled CurseForge key, no automated addon install.** Install is a deep
  link the player's CurseForge app resolves (``curseforge://install?addonId=…``),
  opened on the game PC through the bridge's allowlisted ``open.url`` command.
* **No scraping behind a login.** Search goes through the configured
  ``web_search`` tool; results are metadata only, never downloads.
* **Honest flags.** ``forever`` is ``True`` only when the snippet says so; it is
  ``None`` ("not stated") otherwise — never a confident ``False``.

Uninstall is the mirror of install but entirely local: it queues an allowlisted
``addon.uninstall`` command and never calls CurseForge (there is nothing to look up
remotely). The bridge resolves the installed folder, requires the in-game Accept, and
moves the folder into its removal backup.

Direct handlers return dicts for testability; ``register()`` wraps them as JSON
strings because Hermes's runtime registry rejects raw dict tool results.
``user_task`` arrives when the dispatcher provides it, which is how quest help
sees the ``[WoW context: …]`` line the adapter prepends to a turn.
"""

from __future__ import annotations

import json
import re
import sys
from functools import wraps
from typing import Any, Callable, Optional

MAX_CANDIDATES = 5
MAX_GUIDE_RESULTS = 4

TOOLSET = "wow"

#: CurseForge Core API — the player's own key, never a bundled one.
CURSEFORGE_API_BASE = "https://api.curseforge.com"
CURSEFORGE_GAME_ID = 1                    # World of Warcraft
CURSEFORGE_FOREVER_VERSION_TYPE_ID = 88568  # "Forever 1.60.1" game version type
CURSEFORGE_KEY_ENV = "WOW_CURSEFORGE_API_KEY"  # profile-scoped Hermes secret

_ADDON_HOSTS = {
    "curseforge.com": "curseforge",
    "www.curseforge.com": "curseforge",
    "addons.wago.io": "wago",
    "wago.io": "wago",
    "www.wowinterface.com": "wowinterface",
    "wowinterface.com": "wowinterface",
}

_FOREVER_RE = re.compile(r"\bforever\b", re.IGNORECASE)
_CONTEXT_RE = re.compile(r"\[WoW context:\s*([^\]]*)\]", re.IGNORECASE)

#: Test seam: ``set_search_fn(lambda query, limit: [...])`` replaces the web search.
_search_fn: Optional[Callable[[str, int], list[dict[str, Any]]]] = None
#: Test seam: ``set_command_sink(fn(character, action, url) -> bool)``.
_command_sink: Optional[Callable[[str, str, str], bool]] = None
#: Test seam: ``set_frame_sink(fn(character, frame) -> bool)`` for install commands.
_frame_sink: Optional[Callable[[str, dict[str, Any]], bool]] = None
#: Test seam: ``set_curseforge_client(fn(path, params) -> dict)``.
_curseforge_client: Optional[Callable[[str, dict[str, Any]], dict[str, Any]]] = None
#: Test seam: ``set_curseforge_key_getter(fn() -> str)``.
_curseforge_key_getter: Optional[Callable[[], str]] = None

_COMMAND_COUNTER = 0


def next_command_id() -> str:
    """Bridge command id, ``c<N>`` (PROTOCOL C)."""
    global _COMMAND_COUNTER
    _COMMAND_COUNTER += 1
    return f"c{_COMMAND_COUNTER}"


def set_search_fn(fn: Optional[Callable[[str, int], list[dict[str, Any]]]]) -> None:
    """Replace the search backend (tests, or an embedding host)."""
    global _search_fn
    _search_fn = fn


def set_frame_sink(fn: Optional[Callable[[str, dict[str, Any]], bool]]) -> None:
    global _frame_sink
    _frame_sink = fn


def set_curseforge_client(fn: Optional[Callable[[str, dict[str, Any]], dict[str, Any]]]) -> None:
    global _curseforge_client
    _curseforge_client = fn


def set_curseforge_key_getter(fn: Optional[Callable[[], str]]) -> None:
    global _curseforge_key_getter
    _curseforge_key_getter = fn


def curseforge_key() -> str:
    """The player's CurseForge key from the *active profile's* Hermes secret scope.

    Resolution is ``agent.secret_scope.get_secret`` only — the same fail-closed
    reader every other Hermes credential uses. There is no plugin-managed key
    file: under a multiplexed gateway a miss returns ``""`` instead of borrowing
    another profile's ``os.environ`` value, and the desktop setup page writes the
    key into the selected profile's own secret store. Never logged, never
    returned to the model.
    """
    if _curseforge_key_getter is not None:
        try:
            return str(_curseforge_key_getter() or "").strip()
        except Exception:
            return ""
    try:
        from agent.secret_scope import get_secret

        return str(get_secret(CURSEFORGE_KEY_ENV, "") or "").strip()
    except Exception:
        pass
    # Hermes's own scoped helper: same ``get_secret`` call, but it also handles
    # the default profile's unscoped startup path. Never a direct os.environ or
    # file read here.
    try:
        from gateway.platforms._shared import get_scoped_secret

        return str(get_scoped_secret(CURSEFORGE_KEY_ENV, "") or "").strip()
    except Exception:
        return ""


def _cf_get(path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """``GET api.curseforge.com`` + path with the player's key (test seam first)."""
    if _curseforge_client is not None:
        return dict(_curseforge_client(path, dict(params or {})) or {})
    import httpx

    key = curseforge_key()
    if not key:
        raise RuntimeError("no CurseForge API key configured")
    response = httpx.get(
        f"{CURSEFORGE_API_BASE}{path}", params=params,
        headers={"x-api-key": key, "Accept": "application/json"}, timeout=15.0)
    response.raise_for_status()
    return dict(response.json() or {})


def _cf_search(q: str, limit: int) -> list[dict[str, Any]]:
    """CurseForge Core API search restricted to the Forever game version."""
    payload = _cf_get("/v1/mods/search", {
        "gameId": CURSEFORGE_GAME_ID,
        "gameVersionTypeId": CURSEFORGE_FOREVER_VERSION_TYPE_ID,
        "searchFilter": q,
        "sortField": 2,      # popularity
        "sortOrder": "desc",
        "pageSize": max(1, min(int(limit), 50)),
    })
    rows = payload.get("data") or []
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        links = row.get("links") or {}
        candidates.append({
            "projectId": row.get("id"),
            "name": re.sub(r"\s+", " ", str(row.get("name") or "")).strip()[:120],
            "source": "curseforge",
            "url": str(links.get("websiteUrl") or ""),
            "forever": True,  # the query filtered on the Forever game version
            "forever_note": "CurseForge lists a Forever-compatible file",
            "snippet": re.sub(r"\s+", " ", str(row.get("summary") or "")).strip()[:280],
            "downloads": row.get("downloadCount"),
            "allow_mod_distribution": row.get("allowModDistribution"),
            "api": "curseforge",
        })
    return [c for c in candidates if c["name"]]


def _default_search(query: str, limit: int) -> list[dict[str, Any]]:
    """Run the Hermes ``web_search`` tool and return its result list."""
    from tools.web_tools import web_search_tool

    raw = web_search_tool(query, limit)
    try:
        payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        return []
    if isinstance(payload, dict):
        if payload.get("success") is False:
            raise RuntimeError(str(payload.get("error") or "web search failed")[:300])
        web = (payload.get("data") or {}).get("web") or []
    else:
        web = []
    return [item for item in web if isinstance(item, dict)]


def _search(query: str, limit: int) -> list[dict[str, Any]]:
    fn = _search_fn or _default_search
    return list(fn(query, limit) or [])


def _host(url: str) -> str:
    match = re.match(r"^[a-z]+://([^/?#]+)", url or "", re.IGNORECASE)
    return (match.group(1) if match else "").lower()


def set_command_sink(fn: Optional[Callable[[str, str, str], bool]]) -> None:
    """Replace the ``open.url`` frame sink (tests, or an embedding host)."""
    global _command_sink
    _command_sink = fn


def _enqueue_open_url(character: str, url: str) -> bool:
    if not character or not url:
        return False
    if _command_sink is not None:
        try:
            return bool(_command_sink(character, "open.url", url))
        except Exception:
            return False
    try:
        from .adapter import enqueue_command  # type: ignore[import-not-found]

        return bool(enqueue_command(character, "open.url", url))
    except Exception:
        pass
    # Loaded by path in tests: find the adapter module already in the cache.
    for module in list(sys.modules.values()):
        path = str(getattr(module, "__file__", "") or "")
        if path.endswith("adapter.py") and hasattr(module, "enqueue_command"):
            try:
                return bool(module.enqueue_command(character, "open.url", url))
            except Exception:
                return False
    return False


def _source_for(url: str) -> str:
    return _ADDON_HOSTS.get(_host(url), "web")


def _forever_flag(*texts: Any) -> Optional[bool]:
    """``True`` when a snippet states Forever support; ``None`` when unstated."""
    for text in texts:
        if isinstance(text, str) and _FOREVER_RE.search(text):
            return True
    return None


def _candidate(item: dict[str, Any]) -> dict[str, Any]:
    url = str(item.get("url") or "").strip()
    title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
    description = re.sub(r"\s+", " ", str(item.get("description") or "")).strip()
    forever = _forever_flag(title, description)
    name = re.sub(r"\s*[-|:]\s*(CurseForge|Wago|WoWInterface).*$", "", title,
                  flags=re.IGNORECASE).strip() or title
    return {
        "name": name[:120],
        "source": _source_for(url),
        "url": url,
        "forever": forever,
        "forever_note": ("mentions Forever" if forever
                         else "snippet does not say; check the page's game-version list"),
        "snippet": description[:280],
    }


def current_character() -> str:
    """The WoW character of the turn this tool runs in, or ``""``.

    Tools have no direct chat handle; the gateway binds the turn's platform and
    chat id as session context (``gateway/session_context.py``), which is the
    same ``Name-Realm`` key the adapter uses.
    """
    try:
        from gateway.session_context import get_session_env

        platform_name = str(get_session_env("HERMES_SESSION_PLATFORM", "") or "").lower()
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "") or ""
    except Exception:
        return ""
    if platform_name != "wow":
        return ""
    try:
        from .modes import normalize_character  # type: ignore[import-not-found]

        return normalize_character(chat_id)
    except Exception:
        pass
    # Loaded by path (tests / non-package host): reuse the already-imported
    # sibling (adapter or modes) instead of putting this folder on sys.path,
    # which would shadow Hermes's own top-level ``tools`` package.
    for module in list(sys.modules.values()):
        path = str(getattr(module, "__file__", "") or "")
        if path.endswith(("adapter.py", "modes.py")) and hasattr(module, "normalize_character"):
            try:
                return module.normalize_character(chat_id)
            except Exception:
                return ""
    return ""


def parse_game_context(text: str) -> dict[str, str]:
    """Parse the ``[WoW context: k=v; …]`` line the adapter prepends, if present."""
    if not isinstance(text, str):
        return {}
    match = _CONTEXT_RE.search(text)
    if not match:
        return {}
    out: dict[str, str] = {}
    for part in match.group(1).split(";"):
        key, sep, value = part.partition("=")
        if sep:
            key = key.strip().lower()
            value = value.strip()
            if key and value:
                out[key] = value
    return out


# ── handlers ────────────────────────────────────────────────────────────────


def wow_addon_search(args: dict[str, Any], **_: Any) -> dict[str, Any]:
    """Search for WoW addons (metadata only) and flag Forever support."""
    query = str((args or {}).get("q") or "").strip()
    if not query:
        return {"ok": False, "error": "q is required (an addon name or what it should do)"}
    if len(query) > 200:
        query = query[:200]
    search_query = f"{query} World of Warcraft Forever addon curseforge OR wago"
    backend_used = "web"
    note = None
    candidates: list[dict[str, Any]] = []

    if curseforge_key():
        try:
            candidates = _cf_search(query, MAX_CANDIDATES)
            backend_used = "curseforge-api"
        except Exception as exc:
            note = ("CurseForge API call failed (%s); fell back to web search."
                    % type(exc).__name__)

    if not candidates:
        try:
            results = _search(search_query, 10)
        except Exception as exc:
            return {"ok": False, "error": f"web search failed: {str(exc)[:200]}"}

        seen: set[str] = set()
        # Prefer real addon hosts; fill the rest with generic web hits.
        ordered = sorted(results, key=lambda item: 0 if _source_for(str(item.get("url") or ""))
                         != "web" else 1)
        for item in ordered:
            url = str(item.get("url") or "").strip()
            if not url or url in seen:
                continue
            candidate = _candidate(item)
            if not candidate["name"]:
                continue
            seen.add(url)
            candidates.append(candidate)
            if len(candidates) >= MAX_CANDIDATES:
                break

    return {
        "ok": True,
        "query": query,
        "backend": backend_used,
        "count": len(candidates),
        "candidates": candidates,
        "note": note or (None if curseforge_key() else
                         "No automatic install in wow mode without a CurseForge key: use "
                         "wow_addon_install (key configured) or wow_addon_guide for the link."),
    }


def _addon_id_from_url(url: str) -> str:
    match = re.search(r"(?:addonId|projectId)=(\d{1,12})", url or "", re.IGNORECASE)
    return match.group(1) if match else ""


def wow_addon_guide(args: dict[str, Any], **_: Any) -> dict[str, Any]:
    """Turn an addon id/url into a CurseForge install deep link plus steps."""
    args = args or {}
    addon_id = str(args.get("addonId") or "").strip()
    url = str(args.get("url") or "").strip()
    if addon_id and not addon_id.isdigit():
        return {"ok": False, "error": "addonId must be the numeric CurseForge project id"}
    if not addon_id and url:
        addon_id = _addon_id_from_url(url)
    if url and not re.match(r"^https?://", url, re.IGNORECASE):
        return {"ok": False, "error": "url must be an http(s) addon page"}
    if not addon_id and not url:
        return {"ok": False, "error": "give addonId (CurseForge project id) or url"}

    deep_link = f"curseforge://install?addonId={addon_id}" if addon_id else ""
    source = _source_for(url) if url else ("curseforge" if addon_id else "unknown")

    instructions = [
        "Open the link on the gaming PC (the deck's CurseForge app handles curseforge://install).",
        "In CurseForge choose the WoW: Forever profile/instance and the newest game-version match.",
        "Let the app write the addon folder, then /reload or restart the client so it loads.",
        "Click the addon's minimap/addon-compartment entry or check /addons to confirm it is enabled.",
    ]
    target = deep_link or url
    character = current_character()
    frame_queued = _enqueue_open_url(character, target) if target and character else False
    return {
        "ok": True,
        "addon_id": addon_id or None,
        "source": source,
        "deep_link": deep_link or None,
        "url": url or None,
        "instructions": instructions,
        "frame_queued": frame_queued,
        "frame_note": ("Queued an open.url command for this character's bridge."
                       if frame_queued else
                       "No live bridge in this session — share the link and the steps instead."),
        "safety": "v1.1 never downloads or installs addons from Hermes; the player confirms in CurseForge.",
    }


def _enqueue_frame(character: str, frame: dict[str, Any]) -> bool:
    if not character or not frame:
        return False
    if _frame_sink is not None:
        try:
            return bool(_frame_sink(character, frame))
        except Exception:
            return False
    try:
        from .adapter import enqueue_frame  # type: ignore[import-not-found]

        return bool(enqueue_frame(character, frame))
    except Exception:
        pass
    for module in list(sys.modules.values()):
        path = str(getattr(module, "__file__", "") or "")
        if path.endswith("adapter.py") and hasattr(module, "enqueue_frame"):
            try:
                return bool(module.enqueue_frame(character, frame))
            except Exception:
                return False
    return False


def _install_guided(project_id: Any, reason: str) -> dict[str, Any]:
    """The safe fallback: a CurseForge deep link plus manual steps, and why."""
    addon_id = str(project_id) if project_id else ""
    deep_link = f"curseforge://install?addonId={addon_id}" if addon_id else None
    return {
        "ok": True,
        "installed": False,
        "mode": "guided",
        "reason": reason,
        "addon_id": addon_id or None,
        "deep_link": deep_link,
        "instructions": [
            "Open the link on the gaming PC (the deck's CurseForge app handles curseforge://install).",
            "Pick the WoW: Forever profile/instance and the newest 1.60.1 file.",
            "Let the app write the addon folder, then /reload or restart the client.",
        ],
        "safety": "No download was attempted; the player installs through CurseForge.",
    }


def wow_addon_install(args: dict[str, Any], **_: Any) -> dict[str, Any]:
    """Resolve a CurseForge file and queue a bridge ``addon.install`` command.

    Requires the player's own CurseForge key (``WOW_CURSEFORGE_API_KEY``) and a
    project that allows third-party distribution. Anything else returns the
    guided CurseForge link with the reason. The key travels to the bridge only
    inside the command payload — never in the reply, never in a log line.
    """
    args = args or {}
    raw_id = args.get("projectId")
    try:
        project_id = int(str(raw_id).strip())
    except (TypeError, ValueError):
        return {"ok": False, "error": "projectId must be the numeric CurseForge project id"}
    if project_id <= 0:
        return {"ok": False, "error": "projectId must be a positive integer"}
    raw_file = args.get("fileId")
    file_id = None
    if raw_file not in (None, ""):
        try:
            file_id = int(str(raw_file).strip())
        except (TypeError, ValueError):
            return {"ok": False, "error": "fileId must be a numeric CurseForge file id"}

    key = curseforge_key()
    if not key:
        return _install_guided(
            project_id, "no CurseForge API key is configured for this profile — "
                        "add one in the plugin settings")

    try:
        project = (_cf_get(f"/v1/mods/{project_id}", {}) or {}).get("data") or {}
    except Exception as exc:
        return _install_guided(project_id, f"CurseForge API error: {type(exc).__name__}")
    if not project:
        return _install_guided(project_id, "CurseForge returned no project for that id")
    if project.get("allowModDistribution") is not True:
        return _install_guided(
            project_id, "the addon author disabled third-party distribution "
                        "(allowModDistribution=false), so Hermes cannot download it")

    try:
        files = (_cf_get(f"/v1/mods/{project_id}/files", {
            "gameVersionTypeId": CURSEFORGE_FOREVER_VERSION_TYPE_ID,
            "pageSize": 50,
        }) or {}).get("data") or []
    except Exception as exc:
        return _install_guided(project_id, f"CurseForge API error: {type(exc).__name__}")
    files = [f for f in files if isinstance(f, dict)]
    if file_id is not None:
        files = [f for f in files if int(f.get("id") or 0) == file_id]
    else:
        files.sort(key=lambda f: str(f.get("fileDate") or ""), reverse=True)
    chosen = files[0] if files else None
    if chosen is None:
        return _install_guided(
            project_id, "no file is published for the Forever (1.60.1) game version")

    download_url = str(chosen.get("downloadUrl") or "").strip()
    if not download_url:
        # CurseForge/api returns null downloadUrl when the author opts out.
        return _install_guided(project_id, "CurseForge returned no download URL for that file")

    sha256 = ""
    for hashes in (chosen.get("hashes") or []):
        if isinstance(hashes, dict) and str(hashes.get("algo")) in {"3", "sha256"}:
            sha256 = str(hashes.get("value") or "")
            break

    character = current_character()
    if not character:
        return _install_guided(
            project_id, "this tool runs outside a WoW chat, so the install command has "
                        "no bridge to reach")

    frame = {
        "kind": "command",
        "id": next_command_id(),
        "action": "addon.install",
        "projectId": project_id,
        "fileId": int(chosen.get("id") or 0),
        "name": re.sub(r"\s+", " ", str(project.get("name") or chosen.get("displayName") or ""))[:120],
        "version": re.sub(r"\s+", " ", str(chosen.get("displayName") or chosen.get("fileName") or ""))[:120],
        "downloadUrl": download_url,
        # Write-only: the bridge needs it for the CDN; never echoed in the reply.
        "apiKey": key,
        "confirm": "player",
    }
    if sha256:
        frame["sha256"] = sha256
    queued = _enqueue_frame(character, frame)
    if not queued:
        return _install_guided(
            project_id, "no live game bridge is attached to this session right now")
    return {
        "ok": True,
        "installed": False,           # the player still confirms in game
        "mode": "bridge-command",
        "queued": True,
        "projectId": project_id,
        "fileId": frame["fileId"],
        "name": frame["name"],
        "version": frame["version"],
        "confirm": "player",
        "note": ("Queued addon.install for the bridge. The player confirms in game before "
                 "anything is written; Hermes does not install without that confirmation."),
    }


def wow_addon_uninstall(args: dict[str, Any], **_: Any) -> dict[str, Any]:
    """Queue a bridge ``addon.uninstall`` command for an installed addon.

    Removal is local, so this tool never calls CurseForge: the bridge resolves the
    installed folder (exact ledger projectId, then exact folder name, then unambiguous
    TOC title/project name), shows the player the normal in-game Accept/Cancel, and only
    then moves the folder into the removal backup. A not-found or ambiguous request
    changes nothing, and the bridge's notice says so.
    """
    args = args or {}
    name = re.sub(r"\s+", " ", str(args.get("name") or "")).strip()
    project_id: Optional[int] = None
    raw_id = args.get("projectId")
    if raw_id not in (None, ""):
        try:
            project_id = int(str(raw_id).strip())
        except (TypeError, ValueError):
            return {"ok": False, "error": "projectId must be the numeric CurseForge project id"}
        if project_id <= 0:
            return {"ok": False, "error": "projectId must be a positive integer"}

    if not name and project_id is None:
        return {"ok": False,
                "error": "give name (the addon folder, project or display name) or projectId"}
    if len(name) > 120:
        name = name[:120]
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        return {"ok": False, "error": "name must be plain text"}

    character = current_character()
    if not character:
        return {
            "ok": False,
            "uninstalled": False,
            "queued": False,
            "mode": "bridge-command",
            "reason": ("this tool runs outside a WoW chat, so the uninstall command has no "
                       "bridge to reach"),
        }

    frame: dict[str, Any] = {
        "kind": "command",
        "id": next_command_id(),
        "action": "addon.uninstall",
        "confirm": "player",
    }
    if name:
        frame["name"] = name
    if project_id is not None:
        frame["projectId"] = project_id
    if not _enqueue_frame(character, frame):
        return {
            "ok": False,
            "uninstalled": False,
            "queued": False,
            "mode": "bridge-command",
            "name": name or None,
            "projectId": project_id,
            "reason": "no live game bridge is attached to this session right now",
        }
    return {
        "ok": True,
        "uninstalled": False,          # the player still accepts, then the bridge moves it
        "queued": True,
        "mode": "bridge-command",
        "name": name or None,
        "projectId": project_id,
        "confirm": "player",
        "note": ("Queued addon.uninstall for the bridge. The player confirms in game; the "
                 "bridge then moves the installed folder into its removal backup (it is never "
                 "permanently deleted). If no installed folder matches, or more than one does, "
                 "the bridge reports that and changes nothing."),
    }


def wow_quest_help(args: dict[str, Any], user_task: str = "", **_: Any) -> dict[str, Any]:
    """Grounded next steps for a quest, using the turn's game context plus web."""

    args = args or {}
    context = parse_game_context(user_task or "")
    quest_id = str(args.get("questId") or context.get("quest_id") or "").strip()
    title = str(args.get("title") or context.get("quest") or context.get("quests") or "").strip()
    objectives = str(context.get("objective") or context.get("objectives") or "").strip()

    if not title and not quest_id:
        return {
            "ok": False,
            "error": ("no quest given: pass title or questId, or send the message with the quest "
                      "tracked so the addon's [WoW context: …] line carries it"),
            "context": context,
        }
    if len(title) > 160:
        title = title[:160]

    parts = [part for part in (title, f"id {quest_id}" if quest_id else "") if part]
    search_query = " ".join(parts) + " quest World of Warcraft Forever walkthrough"
    try:
        results = _search(search_query, 8)
    except Exception as exc:
        return {"ok": False, "error": f"web search failed: {str(exc)[:200]}",
                "quest": {"id": quest_id or None, "title": title or None}}

    guidance: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in results:
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        guidance.append({
            "title": re.sub(r"\s+", " ", str(item.get("title") or "")).strip()[:160],
            "url": url,
            "snippet": re.sub(r"\s+", " ", str(item.get("description") or "")).strip()[:320],
            "forever": _forever_flag(item.get("title"), item.get("description")),
        })
        if len(guidance) >= MAX_GUIDE_RESULTS:
            break

    return {
        "ok": True,
        "quest": {
            "id": quest_id or None,
            "title": title or None,
            # Quoted verbatim from the addon's context line — never paraphrased.
            "objectives_quoted": objectives or None,
        },
        "context": context,
        "guidance": guidance,
        "answer_hint": ("Lead with the next concrete step (where to go / what to kill / who to talk "
                        "to), then one line of why. Use the quoted objectives, not invented text."),
        "note": ("Sources may describe Classic or retail; anything marked forever:false/null needs "
                 "an in-game check (/way, /tar, the quest log)."),
    }


# ── schemas + registration ──────────────────────────────────────────────────

SCHEMAS: dict[str, dict[str, Any]] = {
    "wow_addon_search": {
        "name": "wow_addon_search",
        "description": ("Find World of Warcraft addons by name or purpose. Returns up to 5 "
                        "candidates with name, host (curseforge/wago/wowinterface/web), url and a "
                        "Forever-support flag. Read-only: never downloads or installs."),
        "parameters": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Addon name or what it should do"},
            },
            "required": ["q"],
            "additionalProperties": False,
        },
    },
    "wow_addon_guide": {
        "name": "wow_addon_guide",
        "description": ("Build a CurseForge install deep link (curseforge://install?addonId=…) and "
                        "step-by-step instructions for an addon, and open the link on the game PC "
                        "through the bridge when the player is in game."),
        "parameters": {
            "type": "object",
            "properties": {
                "addonId": {"type": "string", "description": "Numeric CurseForge project id"},
                "url": {"type": "string", "description": "Addon page URL (https) when no id is known"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "wow_addon_install": {
        "name": "wow_addon_install",
        "description": ("Install a CurseForge addon through the game bridge (player confirms in "
                        "game). Requires a CurseForge API key configured for this profile and an "
                        "addon that allows distribution; otherwise returns the guided "
                        "curseforge:// link and the reason."),
        "parameters": {
            "type": "object",
            "properties": {
                "projectId": {"type": "string", "description": "Numeric CurseForge project id"},
                "fileId": {"type": "string", "description": "Optional numeric file id; newest "
                                                             "Forever-compatible file is used"},
            },
            "required": ["projectId"],
            "additionalProperties": False,
        },
    },
    "wow_addon_uninstall": {
        "name": "wow_addon_uninstall",
        "description": ("Uninstall an installed WoW addon through the game bridge (the player "
                        "confirms in game). Give the folder, project or display name, or the "
                        "numeric CurseForge project id. The bridge moves the folder into its "
                        "removal backup (never deletes it) and changes nothing if the addon is "
                        "not installed or the name is ambiguous. Never calls CurseForge."),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Installed addon folder, project or "
                                                          "display name"},
                "projectId": {"type": "string", "description": "Numeric CurseForge project id "
                                                               "when known"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "wow_quest_help": {
        "name": "wow_quest_help",
        "description": ("Grounded next-step guidance for a quest in WoW: Forever. Uses the quest "
                        "name/id plus the [WoW context: …] line on the player's message (objectives "
                        "are quoted verbatim) and web sources."),
        "parameters": {
            "type": "object",
            "properties": {
                "questId": {"type": "string", "description": "Quest id when known"},
                "title": {"type": "string", "description": "Quest name when known"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}

HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "wow_addon_search": wow_addon_search,
    "wow_addon_guide": wow_addon_guide,
    "wow_addon_install": wow_addon_install,
    "wow_addon_uninstall": wow_addon_uninstall,
    "wow_quest_help": wow_quest_help,
}


def register(ctx) -> list[str]:
    """Register the WoW-only tools on a ``PluginContext``; returns their names."""
    for name in ("wow_addon_search", "wow_addon_guide", "wow_addon_install",
                 "wow_addon_uninstall", "wow_quest_help"):
        # Hermes's tool registry rejects raw dict results at runtime
        # (`Tool handler returned unsupported result type: dict`). Keep the direct
        # Python handlers dict-shaped for unit tests, but serialize at the registry
        # boundary so the model actually receives the search/install outcome.
        handler = HANDLERS[name]

        @wraps(handler)
        def json_handler(args: dict[str, Any], *, _handler=handler, **kwargs: Any) -> str:
            return json.dumps(_handler(args, **kwargs), ensure_ascii=False)

        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=SCHEMAS[name],
            handler=json_handler,
            description=SCHEMAS[name]["description"],
            emoji="⚔️",
        )
    return list(HANDLERS)
