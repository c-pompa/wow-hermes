"""``dashboard/plugin_api.py`` — the wow package backend, mounted at ``/api/plugins/wow/``.

FastAPI wrapper only: validation, the thread hop off the gateway event loop, and
HTTP status mapping. All real work lives in ``backend_setup.py`` (unit-tested
against a throwaway profile).

Contract (research 07 §3.5):

``GET /status?profile=wow&character=Name-Realm``
  → ``{ok, profile, profile_exists, plugin_enabled, legacy_plugin_enabled,
       platform_enabled, platform_toolsets, known_plugin_toolsets, default_mode,
       mode, modes, token_set, curseforge_key_set, curseforge_key_scope,
       connected, listener, listening, bound, last_bridge_hello, last_hello,
       last_seen, health_error?, character, adapter}`` — never the token or key.

  ``connected`` / ``listener`` are the desktop's two badges, answered by a live
  ``GET http://<host>:<port>/v1/wow/health`` on the *configured* endpoint (bearer
  from this profile's ``WOW_BRIDGE_TOKEN``; ≤1s timeout). ``connected`` is the
  bridge's own ``connected`` flag; ``listener`` is true when anything answers on
  the port. ``listening`` is kept as an alias of ``listener``. When the endpoint
  is unreachable, ``connected`` is false, ``listener`` falls back to the TCP
  probe, and ``health_error`` carries a short reason (``unreachable``,
  ``unauthorized``, ``http 500``, …) with no secret material.

``POST /setup``  body ``{character, profile_name="wow", default_mode="standard",
                        host="127.0.0.1", port=8791, rotate_token=false,
                        curseforge_api_key=""}``
  → ``{ok, profile, character, created, host, port, default_mode, listening,
       restart_required, curseforge_key, curseforge_key_scope, steps[], token_once?}``.
  ``token_once`` is present only when the token was generated for a new profile or
  ``rotate_token`` was true. A non-empty ``curseforge_api_key`` is saved as the
  **selected profile's** ``WOW_CURSEFORGE_API_KEY`` Hermes secret (while the
  profile home override is installed); blank leaves it unchanged and ``"clear"``
  removes it from that profile only. There is no plugin-managed key file.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

# Hermes mounts dashboard/plugin_api.py by file path, not as ``wow.dashboard.plugin_api``.
# Put the package's parent on sys.path so ``wow.backend_setup`` and its relative imports resolve
# (the isolated desktop backend returned 404 / "No module named 'wow'" before this).
_PACKAGE_PARENT = str(Path(__file__).resolve().parents[2])
if _PACKAGE_PARENT not in sys.path:
    sys.path.insert(0, _PACKAGE_PARENT)

from wow import backend_setup

router = APIRouter()


class SetupBody(BaseModel):
    character: str = Field(description="Character key Name-Realm (or Name with realm)")
    realm: str = ""
    profile_name: str = "wow"
    default_mode: str = "standard"
    host: str = "127.0.0.1"
    port: int = 8791
    rotate_token: bool = False
    # Write-only: "" leaves the stored CurseForge key unchanged, "clear" removes it.
    curseforge_api_key: str = ""


@router.get("/status")
async def get_status(
    profile: str = Query("wow", alias="profile"),
    character: str = Query("", alias="character"),
) -> dict:
    try:
        return await asyncio.to_thread(backend_setup.status, profile_name=profile,
                                       character=character)
    except ValueError as exc:  # invalid profile name
        raise HTTPException(400, str(exc)) from None


@router.post("/setup")
async def post_setup(body: SetupBody) -> dict:
    payload = body.model_dump()
    try:
        return await asyncio.to_thread(backend_setup.setup, payload)
    except backend_setup.SetupError as exc:
        raise HTTPException(400, str(exc)) from None
