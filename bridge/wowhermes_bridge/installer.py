"""`wowhermes-bridge install`: addon + 256 load-on-demand slot addons + ctl/ + pools.

Idempotent by construction: every file is compared to what is on disk, so a second run
reports ``created=0 kept=N``. ``--dry-run`` reports exactly the same counts without writing
anything, which is what the coordinator needs before touching a real ``G:\\World of Warcraft``
install.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .slots import SILENT_WAV, slot_addon_name

INTERFACE = 16001
INBOX_STUB = "WoWHermes_Slot = nil\n"
SLOT_LUA = "WoWHermes_Slot = nil\n"


def slot_toc(index: int) -> str:
    return (
        f"## Interface: {INTERFACE}\n"
        "## LoadOnDemand: 1\n"
        "## Dependencies: WoWHermes\n"
        f"## X-WoWHermes-Slot: {index}\n"
        f"## Title: WoW Hermes slot {index}\n"
        "Slot.lua\n"
    )


@dataclass
class InstallReport:
    wow_dir: Path
    addons_dir: Path
    addon_dir: Path
    addon_source: Path | None
    slot_count: int
    created: int = 0
    kept: int = 0
    updated: int = 0
    dirs_created: int = 0
    dry_run: bool = False
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.created + self.updated > 0

    def summary(self) -> str:
        verb = "would create" if self.dry_run else "created"
        return (
            f"{verb} {self.created} files in {self.dirs_created} new directories, "
            f"updated {self.updated}, kept {self.kept} ({self.slot_count} slots)"
        )

    def lines(self) -> list[str]:
        out = [
            f"wow_dir:      {self.wow_dir}",
            f"addons_dir:   {self.addons_dir}",
            f"addon source: {self.addon_source if self.addon_source else '(not found)'}",
            f"slot addons:  {self.slot_count}",
            f"result:       {self.summary()}",
        ]
        out.extend(f"warn: {w}" for w in self.warnings)
        return out


def find_addon_source(explicit: os.PathLike[str] | str | None = None, repo_root: Path | None = None) -> Path | None:
    """The addon checkout to copy: explicit path, $WOWHERMES_ADDON_SRC, or <repo>/addon/WoWHermes."""
    if explicit:
        path = Path(explicit)
        return path if path.is_dir() else None
    env = os.environ.get("WOWHERMES_ADDON_SRC")
    if env:
        path = Path(env)
        return path if path.is_dir() else None
    root = repo_root or Path(__file__).resolve().parents[2]
    candidate = root / "addon" / "WoWHermes"
    return candidate if candidate.is_dir() else None


class _Writer:
    """Counts created/kept/updated and optionally skips the actual write (dry run)."""

    def __init__(self, report: InstallReport):
        self.report = report

    def file(self, path: Path, data: bytes, *, label: str | None = None) -> None:
        name = label or str(path)
        if path.exists():
            try:
                if path.read_bytes() == data:
                    self.report.kept += 1
                    return
                self.report.updated += 1
                self.report.actions.append(f"update {name}")
            except OSError:
                self.report.updated += 1
                self.report.actions.append(f"rewrite(unreadable) {name}")
        else:
            self.report.created += 1
            self.report.actions.append(f"create {name}")
        if self.report.dry_run:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        from .slots import atomic_write_bytes

        atomic_write_bytes(path, data)

    def directory(self, path: Path) -> None:
        if path.is_dir():
            return
        self.report.dirs_created += 1
        self.report.actions.append(f"mkdir {path}")
        if not self.report.dry_run:
            path.mkdir(parents=True, exist_ok=True)


def install(
    wow_dir: os.PathLike[str] | str,
    *,
    addon_source: os.PathLike[str] | str | None = None,
    slot_count: int = 256,
    dry_run: bool = False,
    repo_root: Path | None = None,
) -> InstallReport:
    wow = Path(wow_dir)
    addons_dir = wow / "Interface" / "AddOns"
    source = find_addon_source(addon_source, repo_root)

    report = InstallReport(
        wow_dir=wow,
        addons_dir=addons_dir,
        addon_dir=addons_dir / "WoWHermes",
        addon_source=source,
        slot_count=slot_count,
        dry_run=dry_run,
    )
    writer = _Writer(report)

    if not wow.is_dir():
        raise FileNotFoundError(f"WoW directory not found: {wow}")
    if source is None:
        report.warnings.append(
            "addon/WoWHermes not found in the checkout and no --addon-src given: "
            "slot addons and doorbell pools are still installed, but the game has no addon yet"
        )

    writer.directory(addons_dir)
    writer.directory(report.addon_dir)

    # 1. copy the addon itself (never the runtime pools, which the bridge owns). The TOC
    #    is authoritative: only files it lists are copied, so a stray file in the checkout
    #    (a pool, an editor backup) can never leak into the game.
    if source is not None:
        tocs = sorted(p for p in source.glob("*.toc") if not p.name.startswith("."))
        root = source.resolve()
        listed: set[str] = {toc.name for toc in tocs}
        for toc in tocs:
            text = toc.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                entry = line.strip()
                if not entry or entry.startswith("#") or "\x00" in entry:
                    continue
                parts = Path(entry).parts
                if ".." in parts:  # never let a TOC line escape the addon folder
                    continue
                try:
                    candidate = (source / entry).resolve()
                    inside = candidate.is_file() and root in candidate.parents
                except (OSError, ValueError):
                    inside = False
                if inside:
                    listed.add(entry)
        # Art the TOC references by path (IconTexture, SetTexture) is not in the file list:
        # ship media/ as well (in-game round 4: Hermes logo icon).
        media = source / "media"
        if media.is_dir():
            for p in media.rglob("*"):
                if p.is_file() and not p.name.startswith("."):
                    listed.add(p.relative_to(source).as_posix())
        if listed:
            files = [(rel, source / rel) for rel in sorted(listed)]
        else:  # no readable TOC: copy the tree, still never the runtime pools
            files = [
                (p.relative_to(source).as_posix(), p) for p in source.rglob("*") if p.is_file()
            ]
        for rel_name, src in files:
            if Path(rel_name).parts and Path(rel_name).parts[0] in ("sig", "ack"):
                continue
            writer.file(report.addon_dir / rel_name, src.read_bytes())
        # Older installs may have carried filler doorbells; we never ship pre-raised ones.
    else:
        report.actions.append("skip addon copy (no source)")

    # 2. Inbox.lua stub — the /reload fallback path, rewritten on every publish.
    writer.file(report.addon_dir / "Inbox.lua", INBOX_STUB.encode())

    # 3. ctl/: valid.wav exists from install, absent.wav is never created by anyone.
    writer.directory(report.addon_dir / "ctl")
    writer.file(report.addon_dir / "ctl" / "valid.wav", SILENT_WAV)

    # 4. doorbell pools, empty.
    for pool in ("sig", "ack"):
        writer.directory(report.addon_dir / pool)

    # 5. the 256 load-on-demand slot addons.
    for index in range(1, slot_count + 1):
        name = slot_addon_name(index)
        directory = addons_dir / name
        writer.directory(directory)
        writer.file(directory / f"{name}.toc", slot_toc(index).encode())
        writer.file(directory / "Slot.lua", SLOT_LUA.encode())

    return report
