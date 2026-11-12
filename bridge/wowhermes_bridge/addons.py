"""Confirmed CurseForge addon install (PROTOCOL C ``addon.install``).

The pipeline is deliberately boring and ordered, because every step is a place where a
malicious or badly packaged archive could hurt a new player:

1. **Validate the payload** (numeric project/file ids, https URL on a CurseForge CDN host,
   a key we never log, an optional sha256).
2. **Download** to ``<wow>/wowhermes_staging`` (same volume as the target, so the final
   step is a rename, never a cross-volume copy) with a hard size cap.
3. **Extract with** :mod:`zipsafe` — zip-slip, allowlist, bomb and ``MZ``/``#!`` checks.
4. **Read the TOC** and classify the target flavour; a Classic-only build is refused.
5. **Back up** an existing folder, then ``os.replace`` the staged folder into place.
6. **Ledger** ``%LOCALAPPDATA%\\wowhermes\\addons.json`` with the rollback information.

If WoW is running nothing is touched in the game: the outcome says a full client restart
is required. The bridge never opens a handle to ``Wow.exe`` — the process list only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import zipsafe
from .net import BlockedURL, FetchError, Fetcher, safe_url

__all__ = [
    "DOWNLOADS",
    "LEDGER_VERSION",
    "REMOVED_DIR",
    "InstallError",
    "InstallOutcome",
    "InstallRequest",
    "UninstallError",
    "UninstallOutcome",
    "check_download_url",
    "enable_names",
    "game_running",
    "install",
    "is_protected",
    "protected_reason",
    "read_ledger",
    "resolve_installed",
    "uninstall",
]

DOWNLOADS = frozenset({"edge.forgecdn.net", "mediafilez.forgecdn.net", "api.curseforge.com"})
MAX_ZIP_BYTES = zipsafe.MAX_COMPRESSED
MAX_STAGING_AGE = 24 * 3600
LEDGER_VERSION = 1
MAX_BACKUPS = 2
MAX_KEY = 512
MAX_URL = 2048
MAX_NAME = 120
#: Removals keep a copy of the folder here — a subdirectory of the install backup root,
#: so one place holds every copy the bridge ever took and pruning stays per addon.
REMOVED_DIR = "removed"
# Bound on the post-install `reload.enable` list: the slot file is 16 KB and a whole
# dependency chain is never that long. The first entry is always the installed folder.
MAX_ENABLE = 8


class InstallError(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class UninstallError(Exception):
    """A refused removal. ``candidates`` carries the ambiguous folder names."""

    def __init__(self, reason: str, detail: str = "", candidates: list[str] | None = None):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
        self.candidates = list(candidates or [])


@dataclass
class UninstallOutcome:
    folder: str
    backup: str
    name: str = ""
    title: str = ""
    project_id: int | None = None
    # Folders the addon should pass to C_AddOns.DisableAddOn before the /reload reminder.
    disable: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        return [f"Removed {self.folder}", f"Backed up to {self.backup}"]


def check_download_url(url: object) -> str | None:
    """``None`` when the addon download URL is allowed, else a stable reason."""
    if not isinstance(url, str) or not url:
        return "missing_download_url"
    if len(url) > MAX_URL:
        return "url_too_long"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return "url_control_chars"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "bad_url"
    if parts.scheme.lower() != "https":
        return "scheme_not_allowed"
    host = (parts.hostname or "").lower()
    if host not in DOWNLOADS:
        return "host_not_allowed"
    if parts.username or parts.password:
        return "userinfo_not_allowed"
    return None


def _check(url: str) -> None:
    reason = check_download_url(url)
    if reason is not None:
        raise BlockedURL(reason, safe_url(url))


@dataclass(frozen=True)
class InstallRequest:
    project_id: int
    file_id: int
    name: str
    version: str
    download_url: str
    api_key: str
    sha256: str | None = None

    def __repr__(self) -> str:  # never leak the key into a traceback or a log line
        return (
            f"InstallRequest(project_id={self.project_id}, file_id={self.file_id}, "
            f"name={self.name!r}, version={self.version!r}, url={safe_url(self.download_url)!r}, "
            "api_key=***)"
        )

    __str__ = __repr__


@dataclass
class InstallOutcome:
    ok: bool
    name: str
    version: str
    action: str = "installed"  # installed | updated | refused
    reason: str | None = None
    folder: str | None = None
    backup: str | None = None
    sha256: str = ""
    target: str = ""
    restart_required: bool = False
    warnings: list[str] = field(default_factory=list)
    # Folders the addon should hand to C_AddOns.EnableAddOn before the /reload
    # reminder: the installed folder first, then its present required dependencies.
    enable: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = []
        if self.ok:
            verb = "Installed" if self.action == "installed" else "Updated"
            out.append(f"{verb} {self.name} {self.version}".strip())
        else:
            out.append(f"{self.name}: install refused ({self.reason})")
        out.extend(self.warnings)
        return out


# ---------------------------------------------------------------- process list


def game_running(*, names: tuple[str, ...] = ("Wow.exe", "WowB.exe")) -> bool:
    """True when the client process exists — process **list** only, never a handle."""
    lowered = {n.lower() for n in names}
    try:
        if sys.platform == "win32":
            completed = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            output = (completed.stdout or "").lower()
        else:
            completed = subprocess.run(
                ["ps", "-A", "-o", "comm="],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            output = (completed.stdout or "").lower()
    except (OSError, subprocess.SubprocessError):
        return False
    return any(name in output for name in lowered)


# ---------------------------------------------------------------- ledger


def read_ledger(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": LEDGER_VERSION, "addons": {}}
    if not isinstance(data, dict) or not isinstance(data.get("addons"), dict):
        return {"version": LEDGER_VERSION, "addons": {}}
    return data


def _write_ledger(path: Path, data: dict) -> None:
    from .slots import atomic_write_bytes

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    atomic_write_bytes(path, payload)


# ---------------------------------------------------------------- pipeline


def _download(req: InstallRequest, staging: Path, fetcher: Fetcher) -> Path:
    staging.mkdir(parents=True, exist_ok=True)
    target = staging / f"download-{uuid.uuid4().hex[:12]}.zip"
    headers = {"x-api-key": req.api_key} if req.api_key else {}
    try:
        result = fetcher.download(
            req.download_url, target, check=_check, headers=headers, max_bytes=MAX_ZIP_BYTES
        )
    except BlockedURL as exc:
        raise InstallError(exc.reason, safe_url(req.download_url)) from exc
    except FetchError as exc:
        raise InstallError(exc.reason, safe_url(req.download_url)) from exc
    if req.sha256 and result.sha256.lower() != req.sha256.lower():
        target.unlink(missing_ok=True)
        raise InstallError("sha256_mismatch", f"{req.sha256[:12]}… != {result.sha256[:12]}…")
    return target


def _prune_backups(backup_root: Path, folder: str) -> None:
    mine = sorted(backup_root.glob(f"{folder}-*"), key=lambda p: p.name, reverse=True)
    for stale in mine[MAX_BACKUPS:]:
        import shutil

        shutil.rmtree(stale, ignore_errors=True)


def _missing_deps(layout: zipsafe.Layout, addons_dir: Path) -> list[str]:
    present = {p.name.lower() for p in addons_dir.iterdir() if p.is_dir()} if addons_dir.is_dir() else set()
    missing = [dep for dep in layout.required_deps if dep.lower() not in present]
    missing.extend(dep for dep in layout.dependencies if dep.lower() not in present and dep not in missing)
    return missing


def enable_names(folder: str, layout: zipsafe.Layout, addons_dir: Path) -> list[str]:
    """Folders the addon should enable before the manual ``/reload`` reminder.

    The installed folder comes first, then the TOC's **required** dependencies
    (``## Dependencies`` / ``## RequiredDeps``). Optional dependencies and folders
    that are not installed are left out: ``C_AddOns.EnableAddOn`` cannot enable
    either, and a missing folder would only produce a useless failure. Names are
    the on-disk folder names, which is what the client's API takes.
    """
    present: dict[str, str] = {}
    if addons_dir.is_dir():
        for entry in sorted(addons_dir.iterdir()):
            if entry.is_dir():
                present.setdefault(entry.name.lower(), entry.name)

    names = [folder]
    seen = {folder.lower()}
    for dep in list(layout.required_deps) + list(layout.dependencies):
        key = str(dep).strip().lower()
        if not key or key in seen:
            continue
        actual = present.get(key)
        if actual is None:
            continue
        seen.add(key)
        names.append(actual)
        if len(names) >= MAX_ENABLE:
            break
    return names


def install(
    req: InstallRequest,
    *,
    addons_dir: Path,
    staging_dir: Path,
    ledger_path: Path,
    fetcher: Fetcher,
    running: bool | None = None,
    now: float | None = None,
) -> InstallOutcome:
    """Download, validate and atomically place one addon. Raises :class:`InstallError`."""
    addons_dir = Path(addons_dir)
    staging_dir = Path(staging_dir)
    moment = time.time() if now is None else now
    if running is None:
        running = game_running()

    _sweep_staging(staging_dir, moment)
    zip_path = _download(req, staging_dir, fetcher)
    unzip_dir = staging_dir / f"unzip-{uuid.uuid4().hex[:12]}"
    try:
        try:
            zipsafe.extract(zip_path, unzip_dir)
        except zipsafe.ZipRejected as exc:
            raise InstallError(exc.reason, exc.detail) from exc

        try:
            layout = zipsafe.read_layout(unzip_dir)
        except zipsafe.ZipRejected as exc:
            raise InstallError(exc.reason, exc.detail) from exc

        flavour = zipsafe.classify_target(layout)
        warnings: list[str] = []
        if flavour == "classic":
            raise InstallError(
                "not_forever",
                f"{layout.name} only ships Classic TOCs ({', '.join(layout.flavor_tocs) or 'no TOC'})",
            )
        if flavour == "other":
            shown = ", ".join(str(v) for v in layout.interfaces) or ", ".join(layout.flavor_tocs)
            warnings.append(
                f"{layout.name} advertises {shown}, not Forever (16001) — it may not load; "
                "the game will ask you to allow out-of-date addons."
            )
        if flavour == "unknown":
            warnings.append(
                f"{layout.name} has no Interface line this bridge can read; installing anyway."
            )
        missing = _missing_deps(layout, addons_dir)
        if missing:
            warnings.append(
                "This addon needs: " + ", ".join(missing) + " — install those too, or it will be disabled."
            )

        folder = layout.name
        destination = addons_dir / folder
        addons_dir.mkdir(parents=True, exist_ok=True)
        backup_root = addons_dir / ".wowhermes_backup"
        backup_path: Path | None = None
        action = "installed"
        if destination.exists():
            backup_root.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(moment)) + "-" + uuid.uuid4().hex[:6]
            backup_path = backup_root / f"{folder}-{stamp}"
            os.replace(destination, backup_path)
            action = "updated"

        try:
            os.replace(layout.folder, destination)
        except OSError as exc:
            # Put the old copy back: a failed rename must never leave the player without
            # the addon they already had.
            if backup_path is not None and not destination.exists():
                try:
                    os.replace(backup_path, destination)
                except OSError:
                    pass
            raise InstallError("move_failed", str(exc)) from exc

        if backup_path is not None:
            _prune_backups(backup_root, folder)

        ledger = read_ledger(ledger_path)
        entry = {
            "name": folder,
            "title": layout.title,
            # The CurseForge project name as the player saw it in search / the tool call.
            "projectName": req.name,
            "source": "curseforge",
            "projectId": req.project_id,
            "fileId": req.file_id,
            "version": req.version,
            "sha256": req.sha256 or "",
            "flavor": flavour,
            "interface": layout.interfaces,
            "installedAt": int(moment),
            "backup": backup_path.name if backup_path is not None else None,
        }
        ledger["addons"][folder] = entry
        ledger["version"] = LEDGER_VERSION
        _write_ledger(ledger_path, ledger)

        return InstallOutcome(
            ok=True,
            name=req.name or folder,
            version=req.version,
            action=action,
            folder=folder,
            backup=backup_path.name if backup_path is not None else None,
            sha256=req.sha256 or "",
            target=str(destination),
            # A new folder is only discovered at client start; an update of a loaded
            # addon is just as unreliable. Either way: restart.
            restart_required=True,
            warnings=warnings,
            enable=enable_names(folder, layout, addons_dir),
        )
    finally:
        zip_path.unlink(missing_ok=True)
        _rmtree(unzip_dir)
        _sweep_staging(staging_dir, moment)


def _rmtree(path: Path) -> None:
    import shutil

    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _sweep_staging(staging_dir: Path, moment: float) -> None:
    """Drop leftovers older than a day; never leave downloaded archives behind."""
    if not staging_dir.is_dir():
        return
    cutoff = moment - MAX_STAGING_AGE
    for entry in staging_dir.iterdir():
        try:
            if entry.stat().st_mtime < cutoff:
                _rmtree(entry) if entry.is_dir() else entry.unlink(missing_ok=True)
        except OSError:
            continue


# ---------------------------------------------------------------- uninstall
#
# Removing an addon is the one action that takes something away from the player, so it is
# built like the install pipeline: resolve an *existing* folder (never a caller-supplied
# path), refuse our own and Blizzard's files, and move — never delete — into a timestamped
# backup. A single match is required; an ambiguous request changes nothing.

#: Folders the bridge owns or Blizzard ships. Checked case-insensitively, before any I/O.
_PROTECTED_NAME = re.compile(r"^(WowHermes|Blizzard_.*)$", re.IGNORECASE)
_SLOT_NAME = re.compile(r"^WowHermes_S\d{3}$", re.IGNORECASE)
_BAD_NAME_CHARS = set('<>:"/\\|?*')
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)


def is_protected(folder: object) -> bool:
    """True for our own folders and every Blizzard one (case-insensitive)."""
    return isinstance(folder, str) and bool(
        _PROTECTED_NAME.match(folder.strip()) or _SLOT_NAME.match(folder.strip())
    )


def protected_reason(folder: object) -> str | None:
    """``None`` when ``folder`` may be removed, else a stable reason.

    This is the name-level guard: :func:`uninstall` additionally resolves the folder from a
    directory listing and rejects symlinks and reparse points, so a caller can never smuggle
    in a path, a traversal or a link to somewhere else.
    """
    if not isinstance(folder, str) or not folder:
        return "bad_name"
    if len(folder) > MAX_NAME:
        return "bad_name"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in folder):
        return "bad_name"
    if folder in (".", "..") or folder.startswith("."):
        return "bad_name"
    if any(ch in _BAD_NAME_CHARS for ch in folder):
        return "bad_name"
    if folder != folder.rstrip(" ."):
        return "bad_name"
    if folder.split(".")[0].upper() in _RESERVED_NAMES:
        return "bad_name"
    if _PROTECTED_NAME.match(folder) or _SLOT_NAME.match(folder):
        return "protected"
    return None


def _is_link(path: Path) -> bool:
    """True for a symlink, a junction, or any Windows reparse point."""
    try:
        if path.is_symlink():
            return True
        if hasattr(path, "is_junction") and path.is_junction():  # Python 3.12+
            return True
        if os.name == "nt":
            attributes = int(getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0))
            if attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
                return True
    except OSError:
        return False
    return False


def _installed_folders(addons_dir: Path) -> dict[str, Path]:
    """Case-insensitive folder name -> real entry directly under ``addons_dir``."""
    if not addons_dir.is_dir():
        return {}
    out: dict[str, Path] = {}
    for entry in sorted(addons_dir.iterdir()):
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        out.setdefault(entry.name.lower(), entry)
    return out


def _entry_title(folder: Path) -> str:
    """The ``## Title`` of a folder's TOC, ``""`` when it cannot be read."""
    try:
        toc = next(iter(sorted(folder.glob("*.toc"))), None)
        if toc is None:
            return ""
        text = toc.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        match = re.match(r"^\s*##\s*Title\s*:\s*(.+?)\s*$", line, re.IGNORECASE)
        if match:
            return match.group(1).strip()[:MAX_NAME]
    return ""


def resolve_installed(
    *,
    name: object = "",
    project_id: object = None,
    addons_dir: Path,
    ledger: dict | None = None,
) -> tuple[Path, str, dict]:
    """Resolve one installed folder, or raise :class:`UninstallError`.

    Order (each step must produce exactly one match):

    1. an exact ledger ``projectId`` match, when a numeric id was given;
    2. exact case-insensitive folder name;
    3. exact case-insensitive TOC title / recorded project name.

    Returns ``(folder_path, declared_name, ledger_entry)``. The folder is always a real
    directory directly under ``addons_dir``; a protected name, a symlink, a reparse point
    or anything that is not an installed folder is refused.
    """
    addons_dir = Path(addons_dir)
    ledger = ledger if isinstance(ledger, dict) else {}
    raw_records = ledger.get("addons")
    records: dict = raw_records if isinstance(raw_records, dict) else {}
    present = _installed_folders(addons_dir)
    wanted_name = str(name or "").strip()[:MAX_NAME]
    wanted_id: int | None
    try:
        wanted_id = int(str(project_id)) if project_id not in (None, "") else None
    except (TypeError, ValueError):
        wanted_id = None

    def result(folder: Path, declared: str) -> tuple[Path, str, dict]:
        reason = protected_reason(folder.name)
        if reason is not None:
            raise UninstallError(reason, folder.name)
        if folder.parent != addons_dir or _is_link(folder):
            raise UninstallError("protected", f"{folder.name} is a link")
        record = records.get(folder.name)
        return folder, declared, record if isinstance(record, dict) else {}

    # 1. ledger projectId (only when that folder is still installed)
    if wanted_id is not None:
        matches = [
            key for key, record in records.items()
            if isinstance(record, dict) and str(record.get("projectId") or "") == str(wanted_id)
        ]
        installed = [key for key in matches if key.lower() in present]
        if len(installed) == 1:
            return result(present[installed[0].lower()], installed[0])
        if len(installed) > 1:
            raise UninstallError("ambiguous", ", ".join(sorted(installed)), sorted(installed))
        if matches:
            # The ledger knows this project, but its folder is gone: never guess by name.
            raise UninstallError("not_installed", f"project {wanted_id}")

    # 2. exact folder name (case-insensitive)
    if wanted_name:
        folder = present.get(wanted_name.lower())
        if folder is not None:
            return result(folder, folder.name)

    # 3. exact TOC title / recorded project name, only when unambiguous
    if wanted_name:
        wanted = wanted_name.lower()
        matches = []
        for folder in present.values():
            raw = records.get(folder.name)
            record: dict = raw if isinstance(raw, dict) else {}
            names = {str(record.get("title") or "").lower(),
                     str(record.get("projectName") or "").lower()}
            title = _entry_title(folder).lower()
            if title:
                names.add(title)
            if wanted in names:
                matches.append(folder)
        if len(matches) == 1:
            return result(matches[0], matches[0].name)
        if len(matches) > 1:
            candidates = sorted(folder.name for folder in matches)
            raise UninstallError("ambiguous", ", ".join(candidates), candidates)

    raise UninstallError("not_installed", wanted_name or (f"project {wanted_id}" if wanted_id else ""))


def uninstall(
    *,
    name: object = "",
    project_id: object = None,
    addons_dir: Path,
    ledger_path: Path,
    now: float | None = None,
) -> UninstallOutcome:
    """Move one installed addon folder into the removal backup and ledger it.

    Raises :class:`UninstallError` for every refusal; in those cases nothing on disk and
    nothing in the ledger changes. On success the folder is absent and the backup exists
    (verified before returning).
    """
    addons_dir = Path(addons_dir)
    ledger_path = Path(ledger_path)
    moment = time.time() if now is None else now
    ledger = read_ledger(ledger_path)

    folder, _declared, record = resolve_installed(
        name=name, project_id=project_id, addons_dir=addons_dir, ledger=ledger
    )
    title = str(record.get("title") or "") or _entry_title(folder)
    raw_project = record.get("projectId")
    project: int | None = int(str(raw_project)) if str(raw_project or "").isdigit() else None

    backup_root = addons_dir / ".wowhermes_backup" / REMOVED_DIR
    try:
        backup_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UninstallError("backup_failed", str(exc)) from exc
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(moment)) + "-" + uuid.uuid4().hex[:6]
    backup_path = backup_root / f"{folder.name}-{stamp}"

    try:
        os.replace(folder, backup_path)
    except OSError as exc:
        # A failed rename must leave the addon exactly where it was.
        if folder.exists() and not backup_path.exists():
            raise UninstallError("move_failed", str(exc)) from exc
        try:
            if backup_path.exists() and not folder.exists():
                os.replace(backup_path, folder)
        except OSError:
            pass
        raise UninstallError("move_failed", str(exc)) from exc

    if folder.exists() or not backup_path.is_dir():
        # The rename reported success but the outcome is not what we promised: put it back.
        try:
            if backup_path.exists() and not folder.exists():
                os.replace(backup_path, folder)
        except OSError:
            pass
        raise UninstallError("move_failed", "source still present or backup missing")

    entry = dict(record)
    entry.update({
        "name": folder.name,
        "title": title,
        "projectId": project,
        "removedAt": int(moment),
        "removedBackup": str(backup_path),
    })
    ledger.setdefault("addons", {})[folder.name] = entry
    ledger["version"] = LEDGER_VERSION
    try:
        _write_ledger(ledger_path, ledger)
    except OSError as exc:
        # No partial outcome: put the folder back and report that nothing was removed.
        try:
            os.replace(backup_path, folder)
        except OSError:
            pass
        raise UninstallError("ledger_failed", str(exc)) from exc

    display = str(record.get("projectName") or title or folder.name)
    return UninstallOutcome(
        folder=folder.name,
        backup=str(backup_path),
        name=display,
        title=title,
        project_id=project,
        disable=[folder.name],
    )
