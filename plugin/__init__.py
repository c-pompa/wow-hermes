"""Import-light package entry point for the ``wow`` Hermes plugin.

One installable folder, three halves (research 07 §1.1):

* ``kind: platform`` — the bridge adapter (``adapter.py``), imported lazily so
  listing plugins never pulls in aiohttp or the gateway platform stack.
* WoW-only tools (``tools.py``, toolset ``wow``) and the ``/mode`` slash command.
* A read-only bundled skill (``skills/wow-player-guide``); the per-character
  profile skill and ``SOUL.md`` are written by the backend at setup time.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["register"]


def register(ctx):
    """Plugin entry point — called by the Hermes plugin system at startup."""
    from .adapter import mode_command, register as _register_platform

    _register_platform(ctx)

    from . import tools

    tools.register(ctx)

    ctx.register_command(
        "mode",
        mode_command,
        description=("Switch this character's toolset: standard | wow (WoW-only tools + web) | "
                     "full (whole toolset, 60 min, /mode full confirm)"),
        args_hint="[standard|wow|full]",
    )

    skill_dir = Path(__file__).resolve().parent / "skills" / "wow-player-guide"
    skill_path = skill_dir / "SKILL.md"
    if skill_path.is_file():
        ctx.register_skill(
            "wow-player-guide",
            skill_path,
            description=("Playing WoW: Forever with Hermes — modes (/mode), quest help, addon "
                         "search/install guidance, and the rules of the in-game channel."),
        )
    return None
