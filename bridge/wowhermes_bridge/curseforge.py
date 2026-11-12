"""The CurseForge desktop app: detect it, or fetch *the official* installer.

Detection never opens a process handle: it looks for ``CurseForge.exe`` in the known
install locations and, failing that, for the registered ``curseforge://`` protocol
handler (a registry key — the app writes it when it installs).

The installer is downloaded only by resolving ``https://www.curseforge.com/download/app``
and only from hosts on the allowlist (``www.curseforge.com``, ``curseforge.com`` and
``*.forgecdn.net``), with every redirect re-checked. It is then handed to the OS with
``os.startfile`` so the installer's own UI (and its UAC prompt) runs — the bridge never
passes ``/S`` and never installs anything silently.
"""

from __future__ import annotations

import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .net import BlockedURL, FetchError, Fetcher, safe_url

__all__ = [
    "DOWNLOAD_PAGE",
    "MAX_INSTALLER_BYTES",
    "AppStatus",
    "InstallerError",
    "check_installer_url",
    "detect_app",
    "download_installer",
]

DOWNLOAD_PAGE = "https://www.curseforge.com/download/app"
#: Installers are ~5-120 MB; the cap is generous but finite.
MAX_INSTALLER_BYTES = 300 * 1024 * 1024
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_URL = 2048

_ALLOWED_EXACT = frozenset({"www.curseforge.com", "curseforge.com"})
_ALLOWED_SUFFIX = ".forgecdn.net"

#: env-var templates, expanded case-insensitively on any OS (see ``_expand``).
KNOWN_PATHS = (
    r"%LOCALAPPDATA%\Programs\CurseForge Windows\CurseForge.exe",
    r"%LOCALAPPDATA%\Programs\CurseForge\CurseForge.exe",
    r"%LOCALAPPDATA%\Programs\Overwolf\CurseForge\CurseForge.exe",
    r"%PROGRAMFILES%\CurseForge\CurseForge.exe",
    r"%PROGRAMFILES(X86)%\CurseForge\CurseForge.exe",
    r"%PROGRAMFILES%\Overwolf\CurseForge\CurseForge.exe",
    r"%PROGRAMFILES(X86)%\Overwolf\CurseForge\CurseForge.exe",
)

_EXE_HREF = re.compile(r"""https://[^\s"'<>]+?\.exe\b""", re.IGNORECASE)
_QUOTED = re.compile(r'"([^"]+\.exe)"', re.IGNORECASE)


class InstallerError(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _expand(template: str, env: dict[str, str] | None = None) -> str:
    lookup = {k.upper(): v for k, v in (env or os.environ).items()}
    expanded = re.sub(r"%([^%]+)%", lambda m: lookup.get(m.group(1).upper(), m.group(0)), template)
    # The templates are Windows paths; on a POSIX dev box they are still probeable.
    return expanded if os.sep == "\\" else expanded.replace("\\", "/")


def check_installer_url(url: object) -> str | None:
    if not isinstance(url, str) or not url:
        return "missing_url"
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
    if host not in _ALLOWED_EXACT and not host.endswith(_ALLOWED_SUFFIX):
        return "host_not_allowed"
    if parts.username or parts.password:
        return "userinfo_not_allowed"
    return None


def _check(url: str) -> None:
    reason = check_installer_url(url)
    if reason is not None:
        raise BlockedURL(reason, safe_url(url))


# ---------------------------------------------------------------- detection


@dataclass(frozen=True)
class AppStatus:
    installed: bool
    path: str | None = None
    via: str | None = None  # "path" | "protocol"

    def line(self) -> str:
        if self.installed:
            where = f" at {self.path}" if self.path else ""
            how = " (registered curseforge:// handler)" if self.via == "protocol" else ""
            return f"installed{where}{how}"
        return "not detected"


def _command_path(value: str) -> str | None:
    match = _QUOTED.search(value)
    if match:
        return match.group(1)
    head = value.strip().split()[0] if value.strip() else ""
    return head if head.lower().endswith(".exe") else None


def protocol_registered() -> tuple[bool, str | None]:
    """Is the ``curseforge`` URL scheme registered? Registry only, no process access."""
    if sys.platform != "win32":
        return False, None
    try:
        import winreg
    except ImportError:  # pragma: no cover - winreg always exists on Windows
        return False, None
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for key_path in (
            r"Software\Classes\curseforge\shell\open\command",
            r"Software\Classes\curseforge\DefaultIcon",
        ):
            try:
                with winreg.OpenKey(root, key_path) as key:
                    value, _kind = winreg.QueryValueEx(key, "")
            except OSError:
                continue
            return True, _command_path(str(value))
    return False, None


def detect_app(
    *,
    paths: list[str] | tuple[str, ...] | None = None,
    env: dict[str, str] | None = None,
    protocol_probe=protocol_registered,
) -> AppStatus:
    candidates = paths if paths is not None else KNOWN_PATHS
    for template in candidates:
        candidate = Path(_expand(str(template), env))
        try:
            if candidate.is_file():
                return AppStatus(True, str(candidate), "path")
        except OSError:
            continue
    try:
        registered, exe = protocol_probe()
    except Exception:  # noqa: BLE001 - a broken registry read must not break the command
        registered, exe = False, None
    if registered:
        return AppStatus(True, exe, "protocol")
    return AppStatus(False)


# ---------------------------------------------------------------- installer


def _link_from_page(html: str) -> str | None:
    candidates: list[str] = []
    for match in _EXE_HREF.finditer(html):
        url = match.group(0).rstrip("\\").replace("&amp;", "&")
        if check_installer_url(url) is None:
            candidates.append(url)
    if not candidates:
        return None
    lowered = [c.lower() for c in candidates]

    def score(pair: tuple[str, str]) -> tuple[int, int]:
        url, low = pair
        points = 0
        if "curseforge" in low:
            points += 4
        if "standalone" in low:
            points += 2
        if "windows" in low or "win" in low:
            points += 1
        return (-points, len(url))

    return sorted(zip(candidates, lowered), key=score)[0][0]


def download_installer(fetcher: Fetcher, dest_dir: Path, *, now: float | None = None) -> Path:
    """Resolve the official download page and save the Windows installer into ``dest_dir``."""
    moment = time.time() if now is None else now
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        page = fetcher.get(
            DOWNLOAD_PAGE, check=_check, max_bytes=MAX_PAGE_BYTES, headers={"Accept": "text/html"}
        )
    except BlockedURL as exc:
        raise InstallerError(exc.reason, safe_url(DOWNLOAD_PAGE)) from exc
    except FetchError as exc:
        raise InstallerError(exc.reason, safe_url(DOWNLOAD_PAGE)) from exc
    if page.status != 200:
        raise InstallerError("download_page_failed", f"HTTP {page.status}")

    link = _link_from_page(page.body.decode("utf-8", "replace"))
    if link is None:
        raise InstallerError("installer_not_found", "no allowlisted .exe link on the download page")

    target = dest_dir / f"CurseForge-{uuid.uuid4().hex[:10]}.exe"
    try:
        result = fetcher.download(link, target, check=_check, max_bytes=MAX_INSTALLER_BYTES)
    except BlockedURL as exc:
        raise InstallerError(exc.reason, safe_url(link)) from exc
    except FetchError as exc:
        raise InstallerError(exc.reason, safe_url(link)) from exc

    with open(result.path, "rb") as fh:
        head = fh.read(2)
    if head != b"MZ":
        result.path.unlink(missing_ok=True)
        raise InstallerError("not_an_installer", safe_url(link))
    return result.path


def prune_downloads(dest_dir: Path, *, keep: int = 1, max_age: float = 24 * 3600,
                    now: float | None = None) -> None:
    moment = time.time() if now is None else now
    if not dest_dir.is_dir():
        return
    files = sorted(dest_dir.glob("CurseForge-*.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
    for index, path in enumerate(files):
        try:
            if index >= keep or moment - path.stat().st_mtime > max_age:
                path.unlink(missing_ok=True)
        except OSError:
            continue
