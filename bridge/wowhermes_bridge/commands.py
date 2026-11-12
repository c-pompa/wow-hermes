"""Allowlisted bridge actions from the Hermes plugin (PROTOCOL C, v1.1).

| action | needs a player confirm | what it does |
|---|---|---|
| ``open.url`` | no | opens an https URL / ``curseforge://install`` deep link |
| ``app.curseforge.status`` | no | reports whether the CurseForge app is installed |
| ``app.curseforge.install`` | **yes** | downloads the official installer and runs it |
| ``addon.install`` | **yes** | downloads one addon zip, validates it, installs it |
| ``addon.uninstall`` | **yes** | moves one installed addon folder to a backup |

Nothing here runs a shell and nothing accepts a filesystem path: URLs are validated
against per-action host allowlists *before* any I/O, and the destination is derived by the
bridge from the archive itself. A confirming action is parked in :mod:`confirms` and
executed by :func:`execute` only after the addon sends a matching ``c`` record.

``addon.uninstall`` is the one action that resolves a caller-supplied *name* against the
installed folders. It never calls CurseForge (removal is local), never accepts a path, and
moves the folder into ``<AddOns>\\.wowhermes_backup\\removed`` instead of deleting it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from . import addons, curseforge
from .addons import InstallError, InstallRequest, UninstallError
from .confirms import ConfirmStore
from .net import Fetcher

__all__ = [
    "ALLOWED_ACTIONS",
    "MAX_URL",
    "CommandResult",
    "ConfirmAnswer",
    "Services",
    "execute",
    "parse_confirm",
    "run",
    "validate_url",
]

MAX_URL = 2048
ALLOWED_ACTIONS = frozenset(
    {
        "open.url",
        "addon.install",
        "addon.uninstall",
        "app.curseforge.status",
        "app.curseforge.install",
    }
)
CONFIRM_REQUIRED = frozenset({"addon.install", "addon.uninstall", "app.curseforge.install"})

_CURSEFORGE_KEYS = frozenset({"addonId", "fileId"})
_COMMAND_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class Services:
    """Everything an action may touch, injected by :class:`~wowhermes_bridge.app.BridgeApp`."""

    character: str
    session: str
    confirms: ConfirmStore
    addons_dir: Path
    staging_dir: Path
    ledger_path: Path
    downloads_dir: Path
    clock: Callable[[], float]
    fetcher: Fetcher
    game_running: Callable[[], bool] = addons.game_running
    opener: Callable[[str], object] | None = None


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    action: str
    detail: str = ""
    reason: str | None = None
    pending: bool = False
    message: str = ""
    data: dict = field(default_factory=dict)

    @property
    def notice(self) -> str:
        """One line for the overlay / bridge log / in-game note."""
        if self.message:
            return self.message
        if self.ok:
            return f"opened {self.detail}"
        return f"command refused ({self.reason})"


@dataclass(frozen=True)
class ConfirmAnswer:
    command_id: str
    ok: bool


def parse_confirm(text: object) -> ConfirmAnswer | None:
    """``id=c17;ok=1`` -> :class:`ConfirmAnswer`; anything malformed -> ``None``."""
    fields: dict[str, str] = {}
    for item in str(text or "").split(";"):
        if not item:
            continue
        key, _, value = item.partition("=")
        fields[key.strip()] = value.strip()
    command_id = fields.get("id", "")
    flag = fields.get("ok", "")
    if not _COMMAND_ID.match(command_id) or flag not in ("0", "1"):
        return None
    return ConfirmAnswer(command_id, flag == "1")


# ---------------------------------------------------------------- open.url


def validate_url(url: object) -> str | None:
    """Return ``None`` when ``url`` is allowed, else a machine-readable reason."""
    if not isinstance(url, str) or url == "":
        return "missing_url"
    if len(url) > MAX_URL:
        return "url_too_long"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return "url_control_chars"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "bad_url"

    scheme = parts.scheme.lower()
    if scheme == "https":
        if not parts.hostname:
            return "missing_host"
        # No credentials in the URL: the OS must never receive a secret-looking URL.
        if parts.username or parts.password:
            return "userinfo_not_allowed"
        return None

    if scheme == "curseforge":
        if parts.netloc.lower() != "install":
            return "bad_curseforge_target"
        query = parse_qs(parts.query, keep_blank_values=True)
        # addonId is required; fileId is an optional version pin. The guide emits
        # curseforge://install?addonId=<project> with no fileId.
        if "addonId" not in query or set(query) - _CURSEFORGE_KEYS:
            return "bad_curseforge_args"
        for key, values in query.items():
            if len(values) != 1 or not values[0].isdigit():
                return "bad_curseforge_args"
        return None

    return "scheme_not_allowed"


def _open_url(frame: dict, *, opener: Callable[[str], object] | None) -> CommandResult:
    action = "open.url"
    url = frame.get("url")
    reason = validate_url(url)
    if reason is not None:
        return CommandResult(False, action, reason=reason)
    assert isinstance(url, str)

    if opener is None:
        opener = getattr(os, "startfile", None)  # noqa: B009 - Windows-only, optional
    if opener is None:
        return CommandResult(False, action, reason="unsupported_platform")
    try:
        opener(url)
    except OSError as exc:  # noqa: BLE001 - surface it as a notice, never crash the loop
        return CommandResult(False, action, reason="open_failed", detail=str(exc))
    return CommandResult(True, action, detail=url)


# ---------------------------------------------------------------- validation


def _command_id(frame: dict) -> str | None:
    value = str(frame.get("id") or "")
    return value if _COMMAND_ID.match(value) else None


def _validate_addon_install(frame: dict) -> str | None:
    for key in ("projectId", "fileId"):
        try:
            value = int(str(frame.get(key) or ""))
        except (TypeError, ValueError):
            return "bad_project_id" if key == "projectId" else "bad_file_id"
        if value <= 0:
            return "bad_project_id" if key == "projectId" else "bad_file_id"
    name = frame.get("name")
    version = frame.get("version")
    if not isinstance(name, str) or not name.strip():
        return "missing_name"
    if len(name) > 120 or len(str(version or "")) > 120:
        return "name_too_long"
    key = frame.get("apiKey")
    if not isinstance(key, str) or not key.strip():
        return "missing_api_key"
    if len(key) > addons.MAX_KEY or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in key):
        return "bad_api_key"
    sha = frame.get("sha256")
    if sha not in (None, "") and (not isinstance(sha, str) or not _SHA256.match(sha)):
        return "bad_sha256"
    return addons.check_download_url(frame.get("downloadUrl"))


def _validate_addon_uninstall(frame: dict) -> str | None:
    """``name`` and/or ``projectId``; no path, no shell, bounded text."""
    name = frame.get("name")
    project = frame.get("projectId")
    has_name = name not in (None, "")
    has_project = project not in (None, "")
    if not has_name and not has_project:
        return "missing_name"
    if has_name:
        if not isinstance(name, str) or not name.strip():
            return "bad_name"
        if len(name) > addons.MAX_NAME or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
            return "bad_name"
        # A *path* is never a name. Character rules for a folder are enforced again when
        # the bridge resolves the real directory; a display name may contain ":" etc.
        stripped = name.strip()
        if "/" in stripped or "\\" in stripped or ".." in stripped or stripped in (".", ".."):
            return "bad_name"
        if addons.is_protected(stripped):
            return "protected"
    if has_project:
        try:
            value = int(str(project))
        except (TypeError, ValueError):
            return "bad_project_id"
        if value <= 0:
            return "bad_project_id"
    return None


def _label(frame: dict, action: str) -> str:
    if action == "addon.install":
        name = str(frame.get("name") or "").strip()
        version = str(frame.get("version") or "").strip()
        return f"{name} {version}".strip()[:120]
    if action == "addon.uninstall":
        name = str(frame.get("name") or "").strip()
        project = str(frame.get("projectId") or "").strip()
        return (name or f"project {project}")[:120] or "this addon"
    if action == "app.curseforge.install":
        return "the CurseForge app"
    return action


# ---------------------------------------------------------------- execution


def _install_addon(frame: dict, services: Services) -> CommandResult:
    action = "addon.install"
    req = InstallRequest(
        project_id=int(str(frame["projectId"])),
        file_id=int(str(frame["fileId"])),
        name=str(frame["name"]).strip(),
        version=str(frame.get("version") or "").strip(),
        download_url=str(frame["downloadUrl"]),
        api_key=str(frame["apiKey"]),
        sha256=(str(frame["sha256"]) if frame.get("sha256") else None),
    )
    try:
        outcome = addons.install(
            req,
            addons_dir=services.addons_dir,
            staging_dir=services.staging_dir,
            ledger_path=services.ledger_path,
            fetcher=services.fetcher,
            running=services.game_running(),
        )
    except InstallError as exc:
        return CommandResult(
            False,
            action,
            reason=exc.reason,
            detail=exc.detail,
            message=(
                f"{req.name} was not installed ({exc.reason.replace('_', ' ')}). "
                "Nothing in your AddOns folder was changed."
            ),
        )
    except OSError as exc:
        return CommandResult(
            False,
            action,
            reason="install_failed",
            message=(
                f"{req.name} was not installed ({type(exc).__name__}). "
                "Nothing in your AddOns folder was changed."
            ),
        )

    lines = outcome.lines()
    lines.append(
        "The game shows Hermes's own reload reminder (Got it / Cancel). Type /reload so the "
        "client picks up the new files; if the addon still does not appear afterward, restart "
        "World of Warcraft once."
    )
    if outcome.backup:
        lines.append(f"The previous version was backed up as {outcome.backup}.")
    return CommandResult(
        True,
        action,
        detail=f"{outcome.folder} ({outcome.action})",
        message=" ".join(line if line.endswith(".") else line + "." for line in lines),
        data={
            "installed": True,
            "name": outcome.name,
            "version": outcome.version,
            "folder": outcome.folder,
            "backup": outcome.backup,
            "restart_required": outcome.restart_required,
            "warnings": outcome.warnings,
            # B.2 `reload.enable`: what the addon should enable before /reload. Omitted
            # when empty so an older addon-side reader sees exactly today's shape.
            **({"enable": list(outcome.enable)} if outcome.enable else {}),
        },
    )


def _uninstall_addon(frame: dict, services: Services) -> CommandResult:
    action = "addon.uninstall"
    name = str(frame.get("name") or "").strip()
    project = frame.get("projectId")
    try:
        outcome = addons.uninstall(
            name=name,
            project_id=project,
            addons_dir=services.addons_dir,
            ledger_path=services.ledger_path,
            now=services.clock(),
        )
    except UninstallError as exc:
        candidates = list(exc.candidates)
        if exc.reason == "not_installed":
            message = (
                f"No installed addon folder matches {name or 'that project'}. "
                "Nothing in your AddOns folder was changed."
            )
        elif exc.reason == "ambiguous":
            shown = ", ".join(candidates) or exc.detail
            message = (
                f"More than one installed addon matches {name or 'that project'}: {shown}. "
                "Nothing was changed — ask again with the exact folder name."
            )
        elif exc.reason in ("protected", "bad_name"):
            message = (
                f"Hermes will not remove {name or 'that folder'}: it is one of its own "
                "folders, a Blizzard folder, or not a plain addon folder name. "
                "Nothing was changed."
            )
        elif exc.reason == "ledger_failed":
            message = (
                f"{name or 'The addon'} was not removed: the removal could not be recorded, "
                "so the folder was put back."
            )
        else:
            message = (
                f"{name or 'The addon'} was not removed ({exc.reason.replace('_', ' ')}). "
                "Your AddOns folder was left intact."
            )
        return CommandResult(
            False,
            action,
            reason=exc.reason,
            detail=exc.detail,
            message=message,
            data={"uninstalled": False, "candidates": candidates},
        )
    except OSError as exc:
        return CommandResult(
            False,
            action,
            reason="uninstall_failed",
            message=(
                f"{name or 'The addon'} was not removed ({type(exc).__name__}). "
                "Your AddOns folder was left intact."
            ),
        )

    return CommandResult(
        True,
        action,
        detail=f"{outcome.folder} -> {outcome.backup}",
        message=(
            f"Removed {outcome.folder}. It was moved to the backup folder, not deleted: "
            f"{outcome.backup}. The game shows Hermes's reload reminder (Got it / Cancel); "
            "type /reload to finish removing it. If it still appears in the AddOns list "
            "afterward, restart World of Warcraft once."
        ),
        data={
            "uninstalled": True,
            "name": outcome.name,
            "folder": outcome.folder,
            "backup": outcome.backup,
            "restart_required": True,
            # B.2 `reload.disable`: what the addon disables before the manual reminder.
            "disable": list(outcome.disable),
        },
    )


def _install_app(services: Services) -> CommandResult:
    action = "app.curseforge.install"
    try:
        installer = curseforge.download_installer(services.fetcher, services.downloads_dir)
    except curseforge.InstallerError as exc:
        return CommandResult(
            False,
            action,
            reason=exc.reason,
            message=(
                "I could not download the CurseForge installer "
                f"({exc.reason.replace('_', ' ')}). Nothing was run."
            ),
        )

    opener = services.opener or getattr(os, "startfile", None)  # noqa: B009 - Windows-only
    if opener is None:
        return CommandResult(
            False,
            action,
            reason="unsupported_platform",
            message="The installer was downloaded, but this machine cannot run a Windows installer.",
        )
    try:
        # Interactive only: the installer's own window (and UAC) does the work.
        opener(str(installer))
    except OSError as exc:
        return CommandResult(
            False,
            action,
            reason="open_failed",
            detail=str(exc),
            message="The installer was downloaded but could not be started. Nothing was installed.",
        )
    curseforge.prune_downloads(services.downloads_dir, keep=1, now=services.clock())
    return CommandResult(
        True,
        action,
        detail=str(installer),
        message=(
            "The CurseForge setup window is open — finish it, then choose 'Manually add a game' "
            "and point it at your _forever_ folder."
        ),
        data={"started": True, "path": str(installer)},
    )


def _status_app(services: Services) -> CommandResult:
    action = "app.curseforge.status"
    status = curseforge.detect_app()
    if status.installed:
        message = f"CurseForge app: {status.line()}."
    else:
        message = (
            "CurseForge app: not detected. Say 'install CurseForge' if you want me to download "
            "and run the official installer."
        )
    return CommandResult(
        True,
        action,
        detail=status.line(),
        message=message,
        data={"installed": status.installed, "path": status.path, "via": status.via},
    )


def execute(frame: dict, services: Services) -> CommandResult:
    """Run a command that was already confirmed (or needs no confirmation)."""
    action = str(frame.get("action") or "")
    if action == "addon.install":
        reason = _validate_addon_install(frame)
        if reason is not None:
            return CommandResult(False, action, reason=reason)
        return _install_addon(frame, services)
    if action == "addon.uninstall":
        reason = _validate_addon_uninstall(frame)
        if reason is not None:
            label = _label(frame, action)
            if reason in ("protected", "bad_name"):
                message = (
                    f"Hermes will not remove {label}: it is one of its own folders, a "
                    "Blizzard folder, or not a plain addon folder name. Nothing was changed."
                )
            else:
                message = f"Uninstall refused ({reason.replace('_', ' ')}); nothing was changed."
            return CommandResult(False, action, reason=reason, message=message)
        return _uninstall_addon(frame, services)
    if action == "app.curseforge.install":
        return _install_app(services)
    if action == "app.curseforge.status":
        return _status_app(services)
    return CommandResult(False, action, reason="unknown_action")


def run(
    frame: dict,
    services: Services | None = None,
    *,
    opener: Callable[[str], object] | None = None,
) -> CommandResult:
    """Validate one outbox ``command`` frame and either run it or park it for confirmation."""
    action = str(frame.get("action") or "")
    if action not in ALLOWED_ACTIONS:
        return CommandResult(False, action, reason="unknown_action")

    if action == "open.url":
        return _open_url(frame, opener=opener)

    if action == "app.curseforge.status":
        if services is None:
            return CommandResult(False, action, reason="bridge_unavailable")
        return _status_app(services)

    # -- actions that need the player's own Accept -------------------------
    if action in CONFIRM_REQUIRED:
        if str(frame.get("confirm") or "") != "player":
            return CommandResult(False, action, reason="confirm_required")
        if services is None:
            return CommandResult(False, action, reason="bridge_unavailable")
        if action == "addon.install":
            reason = _validate_addon_install(frame)
            if reason is not None:
                return CommandResult(False, action, reason=reason)
        if action == "addon.uninstall":
            reason = _validate_addon_uninstall(frame)
            if reason is not None:
                return CommandResult(False, action, reason=reason)
        command_id = _command_id(frame)
        if command_id is None:
            return CommandResult(False, action, reason="bad_command_id")
        label = _label(frame, action)
        services.confirms.request(
            character=services.character,
            session=services.session,
            command_id=command_id,
            action=action,
            label=label,
            frame=frame,
        )
        verb = "uninstall" if action == "addon.uninstall" else "install"
        return CommandResult(
            False,
            action,
            reason="awaiting_confirm",
            pending=True,
            message=(
                f"Hermes wants to {verb} {label}. Press Accept in the in-game popup to allow it "
                "(Cancel does nothing)."
            ),
            data={"confirm": True, "commandId": command_id, "label": label},
        )

    return CommandResult(False, action, reason="unknown_action")
