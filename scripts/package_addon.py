#!/usr/bin/env python3
"""Package the WoWHermes addon into dist/WoWHermes-<version>.zip.

Stdlib only. The archive contains exactly what the TOC declares:

    WoWHermes/WoWHermes.toc
    WoWHermes/<every TOC-listed file>

Runtime directories created by the bridge installer (ctl/, sig/, ack/) and the
LoadOnDemand slot addons (WoWHermes_S001..S256) are NOT part of this archive --
the bridge installs them.

Version resolution: `--version`, else the TOC `## Version:` field, else
`git describe --tags --always --dirty`.

Usage:
    python3 scripts/package_addon.py
    python3 scripts/package_addon.py --version 0.1.0 --out build/dist
    python3 scripts/package_addon.py --check-only   # validate TOC/files only
    python3 scripts/package_addon.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

ADDON_NAME = "WoWHermes"
TOC_NAME = ADDON_NAME + ".toc"
# Fixed timestamp => byte-identical archives for identical inputs.
FIXED_DATE = (2026, 1, 1, 0, 0, 0)
VERSION_RE = re.compile(r"^##\s*Version:\s*(.+?)\s*$", re.IGNORECASE)
INTERFACE_RE = re.compile(r"^##\s*Interface:\s*(\S+)\s*$", re.IGNORECASE)


class PackageError(Exception):
    """A user-facing packaging failure (bad TOC, missing file, no version)."""


@dataclass
class Toc:
    path: Path
    interface: str | None = None
    version: str | None = None
    files: list[str] = field(default_factory=list)


def repo_root() -> Path:
    """The addon repo root (one level above scripts/)."""
    return Path(__file__).resolve().parent.parent


def parse_toc(toc_path: Path) -> Toc:
    if not toc_path.is_file():
        raise PackageError(f"TOC not found: {toc_path}")
    toc = Toc(path=toc_path)
    seen: set[str] = set()
    for lineno, raw in enumerate(toc_path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            match = VERSION_RE.match(line)
            if match:
                toc.version = match.group(1)
            match = INTERFACE_RE.match(line)
            if match:
                toc.interface = match.group(1)
            continue
        if "\x00" in line:
            raise PackageError(f"{toc_path}:{lineno}: NUL byte in file list")
        # TOC file entries are relative to the addon folder, forward or back slash.
        entry = line.replace("\\", "/")
        if entry.startswith("/") or ".." in entry.split("/"):
            raise PackageError(f"{toc_path}:{lineno}: TOC entry escapes the addon folder: {line}")
        if entry.lower() in seen:
            raise PackageError(f"{toc_path}:{lineno}: duplicate TOC entry: {line}")
        seen.add(entry.lower())
        toc.files.append(entry)
    if not toc.files:
        raise PackageError(f"{toc_path}: TOC lists no files")
    return toc


def resolve_version(toc: Toc, explicit: str | None, root: Path) -> str:
    if explicit:
        return explicit.strip()
    if toc.version:
        return toc.version
    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        out = None
    if out is not None and out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip()
    raise PackageError(
        "no version: add '## Version:' to the TOC, pass --version, or make the repo a git checkout"
    )


def safe_version(version: str) -> str:
    """Filesystem-safe version tag (tags like v1.2-3-gabc become v1.2-3-gabc)."""
    cleaned = re.sub(r"[^A-Za-z0-9._+-]+", "_", version).strip("._")
    if not cleaned:
        raise PackageError(f"unusable version string: {version!r}")
    return cleaned


def check_files(addon_dir: Path, toc: Toc) -> list[tuple[str, Path]]:
    missing = [name for name in toc.files if not (addon_dir / name).is_file()]
    if missing:
        raise PackageError("TOC lists missing file(s): " + ", ".join(missing))
    files = [(name, addon_dir / name) for name in toc.files]
    media = addon_dir / "media"  # art referenced by path (IconTexture / SetTexture), not TOC-listed
    if media.is_dir():
        files += sorted((p.relative_to(addon_dir).as_posix(), p) for p in media.rglob("*")
                        if p.is_file() and not p.name.startswith("."))
    return files


def build_zip(root: Path, toc: Toc, version: str, out_dir: Path) -> Path:
    addon_dir = root / "addon" / ADDON_NAME
    entries = check_files(addon_dir, toc)
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{ADDON_NAME}-{safe_version(version)}.zip"

    tmp_path = zip_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        members = [(TOC_NAME, toc.path)] + entries
        for arcname, source in members:
            info = zipfile.ZipInfo(f"{ADDON_NAME}/{arcname}", date_time=FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0o644 << 16
            data = source.read_bytes()
            zf.writestr(info, data)
    os.replace(tmp_path, zip_path)
    return zip_path


def zip_listing(zip_path: Path) -> list[dict]:
    rows = []
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise PackageError(f"corrupt archive member: {bad}")
        for info in zf.infolist():
            rows.append(
                {
                    "name": info.filename,
                    "bytes": info.file_size,
                    "compressed": info.compress_size,
                    "crc32": f"{info.CRC:08x}",
                }
            )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the WoWHermes addon zip.")
    parser.add_argument("--root", type=Path, default=None, help="repo root (default: parent of scripts/)")
    parser.add_argument("--version", default=None, help="override the TOC/git version")
    parser.add_argument("--out", type=Path, default=None, help="output directory (default: <root>/dist)")
    parser.add_argument("--check-only", action="store_true", help="validate the TOC and file list, write nothing")
    parser.add_argument("--json", action="store_true", help="machine-readable result on stdout")
    args = parser.parse_args(argv)

    root = (args.root or repo_root()).resolve()
    out_dir = (args.out or (root / "dist")).resolve()
    try:
        toc = parse_toc(root / "addon" / ADDON_NAME / TOC_NAME)
        entries = check_files(root / "addon" / ADDON_NAME, toc)
        version = resolve_version(toc, args.version, root)
        if args.check_only:
            result = {
                "ok": True,
                "checkOnly": True,
                "toc": str(toc.path),
                "interface": toc.interface,
                "version": version,
                "files": [name for name, _ in entries],
            }
            print(json.dumps(result, indent=2) if args.json else
                  f"OK: {TOC_NAME} lists {len(entries)} file(s), all present (version {version})")
            return 0
        zip_path = build_zip(root, toc, version, out_dir)
        listing = zip_listing(zip_path)
    except PackageError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(
            {
                "ok": True,
                "zip": str(zip_path),
                "version": version,
                "interface": toc.interface,
                "entries": listing,
            },
            indent=2,
        ))
        return 0

    print(f"built {zip_path} ({zip_path.stat().st_size} bytes, version {version}, interface {toc.interface})")
    for row in listing:
        print(f"  {row['name']:<40} {row['bytes']:>8}  {row['compressed']:>7}  {row['crc32']}")
    print(f"{len(listing)} entries: TOC + {len(toc.files)} listed file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
