"""Configuration: ``%LOCALAPPDATA%\\wowhermes\\config.toml`` and install discovery."""

from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_GATEWAY = "http://127.0.0.1:8791"
DEFAULT_SLOT_COUNT = 256
TOKEN_ENV = "WOWHERMES_TOKEN"
CONFIG_ENV = "WOWHERMES_CONFIG"

#: Paths probed when no ``wow_dir`` is configured (doc 02 §13 / doc 06 §9.8).
WOW_PROBES = (
    r"C:\Program Files (x86)\World of Warcraft\_forever_",
    r"C:\Program Files (x86)\World of Warcraft\_forever_",
    r"C:\Program Files\World of Warcraft\_forever_",
    r"D:\World of Warcraft\_forever_",
)


def config_dir() -> Path:
    if os.environ.get("WOWHERMES_HOME"):
        return Path(os.environ["WOWHERMES_HOME"])
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "wowhermes"


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV) or (config_dir() / "config.toml"))


def find_wow_dirs() -> list[Path]:
    """Every ``_forever_`` install that exists on this machine."""
    found: list[Path] = []
    env = os.environ.get("WOWHERMES_WOW_DIR")
    if env:
        found.append(Path(env))
    for probe in WOW_PROBES:
        found.append(Path(probe))
    # G: / D: / C: siblings of the probe roots (a second install next door).
    for probe in WOW_PROBES:
        parent = Path(probe).parent
        if parent.is_dir():
            for child in parent.iterdir():
                if child.name.startswith("_") and child.is_dir():
                    found.append(child)
    seen: set[str] = set()
    unique: list[Path] = []
    for path in found:
        key = str(path).lower()
        if key not in seen and path.is_dir():
            seen.add(key)
            unique.append(path)
    return unique


def detect_account(wow_dir: Path) -> str:
    """Newest ``WTF\\Account\\<ACC>`` by mtime (doc 02 open question 1), ``""`` when absent."""
    account_root = Path(wow_dir) / "WTF" / "Account"
    if not account_root.is_dir():
        return ""
    candidates = [p for p in account_root.iterdir() if p.is_dir() and p.name != "SavedVariables"]
    if not candidates:
        return ""
    candidates.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    return candidates[0].name


def read_config_wtf(wow_dir: Path) -> dict[str, str]:
    """``WTF\\Config.wtf`` as a dict of ``SET`` values (used for gxWindow warnings)."""
    path = Path(wow_dir) / "WTF" / "Config.wtf"
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0] == "SET":
            values[parts[1]] = parts[2].strip().strip('"')
    return values


def windowed_fullscreen(wow_dir: Path) -> bool | None:
    """True when ``gxWindow`` is set (Windowed / Windowed (Fullscreen)); None if unknown."""
    values = read_config_wtf(wow_dir)
    if "gxWindow" not in values:
        return None
    return values["gxWindow"] not in ("0", "")


@dataclass
class Config:
    wow_dir: str = ""
    account: str = ""
    gateway_url: str = DEFAULT_GATEWAY
    token: str = ""
    character: str = ""
    transport: str = "strip"  # "strip" | "reload"
    overlay: bool = False  # in-game window is primary (user, 2026-11-09); overlay is opt-in
    slot_count: int = DEFAULT_SLOT_COUNT
    path: Path | None = field(default=None, repr=False, compare=False)

    # -- derived paths -----------------------------------------------------
    @property
    def wow(self) -> Path:
        return Path(self.wow_dir)

    @property
    def addons_dir(self) -> Path:
        return self.wow / "Interface" / "AddOns"

    @property
    def addon_root(self) -> Path:
        return self.addons_dir / "WoWHermes"

    @property
    def savedvars_path(self) -> Path:
        account = self.account or detect_account(self.wow)
        return self.wow / "WTF" / "Account" / account / "SavedVariables" / "WoWHermes.lua"

    @property
    def transcripts_path(self) -> Path:
        return config_dir() / "transcripts.json"

    @property
    def addons_ledger_path(self) -> Path:
        """Our own bookkeeping of installed addons (never CurseForge API metadata)."""
        return config_dir() / "addons.json"

    @property
    def downloads_dir(self) -> Path:
        """Where the CurseForge *installer* lands before the player runs it."""
        return config_dir() / "downloads"

    @property
    def staging_dir(self) -> Path:
        """Addon downloads/unpacks live beside the game so the final move is a rename."""
        return self.wow / "wowhermes_staging"

    # -- secrets -----------------------------------------------------------
    def effective_token(self) -> str:
        return os.environ.get(TOKEN_ENV) or self.token

    # -- io ----------------------------------------------------------------
    def save(self, path: Path | None = None) -> Path:
        target = Path(path or self.path or config_path())
        target.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data.pop("path", None)
        lines = ["# wowhermes-bridge configuration"]
        for key, value in data.items():
            if isinstance(value, bool):
                literal = "true" if value else "false"
            elif isinstance(value, (int, float)):
                literal = str(value)
            else:
                literal = json.dumps(str(value))
            lines.append(f"{key} = {literal}")
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.path = target
        return target

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        target = Path(path or config_path())
        data: dict = {}
        if target.is_file():
            # utf-8-sig: Notepad / PowerShell 5.1 `Set-Content -Encoding utf8` prepend a BOM (seen live)
            data = tomllib.loads(target.read_text(encoding="utf-8-sig"))
        known = {f for f in cls.__dataclass_fields__ if f != "path"}  # type: ignore[attr-defined]
        config = cls(**{k: v for k, v in data.items() if k in known})
        config.path = target
        if not config.wow_dir:
            found = find_wow_dirs()
            if found:
                config.wow_dir = str(found[0])
        if config.wow_dir and not config.account:
            config.account = detect_account(config.wow)
        if not config.character and config.account:
            config.character = ""  # learned from the hello frame; account != character
        return config

    def status_lines(self) -> list[str]:
        token = self.effective_token()
        return [
            f"config:       {self.path or config_path()}{'' if (self.path or config_path()).is_file() else ' (missing)'}",
            f"wow_dir:      {self.wow_dir or '(not found)'}",
            f"account:      {self.account or '(not detected)'}",
            f"addons_dir:   {self.addons_dir}",
            f"savedvars:    {self.savedvars_path}",
            f"gateway:      {self.gateway_url}",
            f"token:        {'set (' + str(len(token)) + ' chars)' if token else 'MISSING'}",
            f"character:    {self.character or '(from hello)'}",
            f"transport:    {self.transport}",
            f"overlay:      {self.overlay}",
        ]
