#!/usr/bin/env python3
"""Build the reproducible WoWHermes release artifacts.

Stdlib only. Output (default ``dist/v<version>/``):

    wowhermes-plugin-v<version>.zip     native Hermes package, rooted at ``wow/``
    WoWHermes-v<version>.zip            addon only (CurseForge-ready: TOC + media)
    wowhermes-bridge-v<version>.zip     bridge source + addon + bootstrap + docs
    SHA256SUMS                          sha256 of every artifact (sha256sum format)
    release-manifest.json               source commit, component versions, per-file sha256
    docs/                               INSTALL.md, RELEASE.md, PROTOCOL.md

The build is deterministic: identical inputs and the same commit produce byte-identical
archives (fixed zip timestamps, sorted entries, no build clock in any file).

Usage:
    python3 scripts/build_release.py --version 0.2.0
    python3 scripts/build_release.py --version 0.2.0 --check-only
    python3 scripts/build_release.py --version 0.2.0 --out build/dist --json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PRODUCT = "WoWHermes"
ADDON_NAME = "WoWHermes"
PLUGIN_NAME = "wow"
BRIDGE_NAME = "wowhermes-bridge"
FIXED_DATE = (2026, 1, 1, 0, 0, 0)
INTEGRITY_DATE = FIXED_DATE
DOC_FILES = ("INSTALL.md", "RELEASE.md")

# Cache/runtime noise that must never reach an artifact.
NOISE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv",
              ".idea", ".vscode"}
NOISE_FILES = {".DS_Store", "Thumbs.db"}
NOISE_SUFFIXES = (".pyc", ".pyo", ".swp", ".tmp", ".log")


class ReleaseError(Exception):
    """A user-facing build failure (bad manifest, missing file, version mismatch)."""


# ---------------------------------------------------------------- helpers ---
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_git(root: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                             timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.strip()


def load_addon_packager(root: Path):
    """Reuse scripts/package_addon.py instead of duplicating TOC validation."""
    path = root / "scripts" / "package_addon.py"
    if not path.is_file():
        raise ReleaseError(f"missing addon packager: {path}")
    spec = importlib.util.spec_from_file_location("wowhermes_package_addon", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import machinery
        raise ReleaseError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses looks the module up in sys.modules while processing fields.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_kv_manifest(path: Path) -> dict[str, str]:
    """Read the flat ``key: value`` head of plugin.yaml (no PyYAML in the stdlib)."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw or raw[0].isspace() or raw.lstrip().startswith("#"):
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", raw)
        if match:
            key, value = match.group(1), match.group(2).strip().strip('"')
            values[key] = value
    return values


def parse_pyproject_version(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    section = re.search(r"^\[project\]$(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    if not section:
        raise ReleaseError(f"{path}: no [project] table")
    match = re.search(r'^version\s*=\s*"([^"]+)"', section.group(1), re.MULTILINE)
    if not match:
        raise ReleaseError(f"{path}: no project version")
    return match.group(1)


def parse_installer_pins(path: Path) -> dict[str, str]:
    """The bootstrap pins are the single source of truth; the manifest records them."""
    text = path.read_text(encoding="utf-8")
    uv = re.search(r"\$script:UvVersion\s*=\s*'([^']+)'", text)
    py = re.search(r"uv python install (\d+\.\d+)", text)
    if not uv or not py:
        raise ReleaseError(f"{path}: cannot read the pinned uv/Python versions")
    return {"uv": uv.group(1), "python": py.group(1)}


def iter_tree(base: Path, *, skip_dirs: set[str] | None = None):
    """Sorted (relative posix path, bytes) pairs, minus cache/runtime noise."""
    skip_dirs = skip_dirs or set()
    files: list[tuple[str, bytes]] = []
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(base)
        parts = set(rel.parts[:-1])
        if parts & NOISE_DIRS or parts & skip_dirs:
            continue
        if rel.name in NOISE_FILES or rel.name.endswith(NOISE_SUFFIXES):
            continue
        files.append((rel.as_posix(), path.read_bytes()))
    files.sort(key=lambda item: item[0])
    return files


# ------------------------------------------------------------- validation ---
def collect_plugin(root: Path, label: str) -> tuple[list[tuple[str, bytes]], dict]:
    # Canonical development checkout: hermes_plugin/wow. Sanitized public mirror: plugin/.
    plugin_dir = root / "hermes_plugin" / PLUGIN_NAME
    if not plugin_dir.is_dir():
        plugin_dir = root / "plugin"
    manifest = plugin_dir / "plugin.yaml"
    if not manifest.is_file():
        raise ReleaseError(f"missing plugin manifest: {manifest}")
    meta = read_kv_manifest(manifest)
    if meta.get("name") != PLUGIN_NAME:
        raise ReleaseError(f"plugin.yaml name is {meta.get('name')!r}, expected {PLUGIN_NAME!r}")
    if meta.get("kind") != "platform":
        raise ReleaseError(f"plugin.yaml kind is {meta.get('kind')!r}, expected 'platform'")
    if meta.get("version") != label:
        raise ReleaseError(
            f"plugin.yaml version {meta.get('version')!r} does not match --version {label!r}"
        )
    for required in ("adapter.py", "__init__.py", "dashboard/plugin_api.py", "desktop/plugin.js"):
        if not (plugin_dir / required).is_file():
            raise ReleaseError(f"plugin package is missing {required}")
    files = iter_tree(plugin_dir, skip_dirs={"tests"})
    files = [(name, data) for name, data in files
             if not (name.startswith("desktop/test_") or name.endswith("_test.mjs"))]
    if not files:
        raise ReleaseError(f"plugin package is empty: {plugin_dir}")
    entries = [(f"{PLUGIN_NAME}/{name}", data) for name, data in files]
    return entries, {
        "version": meta.get("version"),
        # manifest_version/api_version are YAML integers; keep them numeric in JSON.
        "manifest_version": int(meta["manifest_version"]) if meta.get("manifest_version", "").isdigit() else meta.get("manifest_version"),
        "api_version": int(meta["api_version"]) if meta.get("api_version", "").isdigit() else meta.get("api_version"),
        "files": len(entries),
    }


def collect_addon(root: Path, packager) -> tuple[list[tuple[str, bytes]], dict]:
    addon_dir = root / "addon" / ADDON_NAME
    toc = packager.parse_toc(addon_dir / f"{ADDON_NAME}.toc")
    listed = packager.check_files(addon_dir, toc)
    entries: list[tuple[str, bytes]] = []
    for name, path in listed:
        entries.append((f"{ADDON_NAME}/{name}", path.read_bytes()))
    # The TOC itself is not in its own file list.
    entries.append((f"{ADDON_NAME}/{ADDON_NAME}.toc", (addon_dir / f"{ADDON_NAME}.toc").read_bytes()))
    entries.sort(key=lambda item: item[0])
    if not any(name.startswith(f"{ADDON_NAME}/media/") for name, _ in entries):
        raise ReleaseError("addon archive has no media/ files (icon art would be missing in game)")
    return entries, {
        "version": toc.version,
        "interface": toc.interface,
        "toc_files": len(toc.files),
        "files": len(entries),
    }


def collect_bridge(root: Path, label: str, addon_entries: list[tuple[str, bytes]]):
    bridge_dir = root / "bridge"
    pyproject = bridge_dir / "pyproject.toml"
    if not pyproject.is_file():
        raise ReleaseError(f"missing {pyproject}")
    if not re.search(r'^name\s*=\s*"wowhermes-bridge"\s*$', pyproject.read_text(encoding="utf-8"),
                     re.MULTILINE):
        raise ReleaseError("bridge/pyproject.toml does not describe wowhermes-bridge")
    if not (bridge_dir / "uv.lock").is_file():
        raise ReleaseError("bridge/uv.lock is required for `uv sync --frozen`")
    if not (bridge_dir / ".python-version").is_file():
        raise ReleaseError("bridge/.python-version is required for a pinned interpreter")
    protocol_copy = bridge_dir / "wowhermes_bridge" / "protocol.py"
    protocol_source = root / "protocol" / "wowhermes_protocol.py"
    if protocol_copy.read_bytes() != protocol_source.read_bytes():
        raise ReleaseError("bridge/wowhermes_bridge/protocol.py drifted from protocol/wowhermes_protocol.py")

    prefix = f"{BRIDGE_NAME}-v{label}"
    entries: list[tuple[str, bytes]] = []
    bridge_files = iter_tree(bridge_dir, skip_dirs={"tests"})
    keep = {".python-version", "pyproject.toml", "uv.lock", "README.md", "NOTICE"}
    for name, data in bridge_files:
        if name.split("/")[0] in keep or name.startswith("wowhermes_bridge/"):
            entries.append((f"{prefix}/bridge/{name}", data))
    if not any(name.endswith("/bridge/uv.lock") for name, _ in entries):
        raise ReleaseError("bridge archive is missing uv.lock")

    bootstrap_dir = root / "scripts" / "bootstrap"
    for script in ("install.ps1", "run.ps1", "stop.ps1", "uninstall.ps1", "README.md"):
        path = bootstrap_dir / script
        if not path.is_file():
            raise ReleaseError(f"missing bootstrap file: {path}")
        entries.append((f"{prefix}/bootstrap/{script}", path.read_bytes()))

    for arcname, data in addon_entries:
        entries.append((f"{prefix}/addon/{arcname}", data))

    for name in ("LICENSE", "NOTICE"):
        path = root / name
        if not path.is_file():
            raise ReleaseError(f"missing {name} at the repository root")
        entries.append((f"{prefix}/{name}", path.read_bytes()))
    entries.append((f"{prefix}/README.md", (bootstrap_dir / "README.md").read_bytes()))
    for doc in ("INSTALL.md",):
        path = root / "docs" / doc
        if not path.is_file():
            raise ReleaseError(f"missing docs/{doc}")
        entries.append((f"{prefix}/docs/{doc}", path.read_bytes()))

    entries.sort(key=lambda item: item[0])
    return entries, {
        "version": parse_pyproject_version(pyproject),
        "python": (bridge_dir / ".python-version").read_text(encoding="utf-8").strip(),
        "files": len(entries),
    }


# ------------------------------------------------------------------- build ---
def write_zip(path: Path, entries: list[tuple[str, bytes]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for arcname, data in sorted(entries, key=lambda item: item[0]):
            info = zipfile.ZipInfo(arcname, date_time=FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    os.replace(tmp, path)


def describe_artifact(path: Path, entries: list[tuple[str, bytes]]) -> dict:
    return {
        "name": path.name,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "files": [
            {"path": arcname, "bytes": len(data), "sha256": sha256_bytes(data)}
            for arcname, data in sorted(entries, key=lambda item: item[0])
        ],
    }


def build(root: Path, label: str, out_root: Path) -> dict:
    packager = load_addon_packager(root)
    plugin_entries, plugin_meta = collect_plugin(root, label)
    addon_entries, addon_meta = collect_addon(root, packager)
    bridge_entries, bridge_meta = collect_bridge(root, label, addon_entries)
    pins = parse_installer_pins(root / "scripts" / "bootstrap" / "install.ps1")

    out_dir = out_root / f"v{label}"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    artifacts = []
    for filename, entries in (
        (f"wowhermes-plugin-v{label}.zip", plugin_entries),
        (f"{ADDON_NAME}-v{label}.zip", addon_entries),
        (f"{BRIDGE_NAME}-v{label}.zip", bridge_entries),
    ):
        path = out_dir / filename
        write_zip(path, entries)
        artifacts.append(describe_artifact(path, entries))

    docs_dir = out_dir / "docs"
    docs_dir.mkdir()
    docs = []
    for doc in DOC_FILES:
        source = root / "docs" / doc
        if not source.is_file():
            raise ReleaseError(f"missing docs/{doc}")
        target = docs_dir / doc
        shutil.copyfile(source, target)
        docs.append({"path": f"docs/{doc}", "bytes": target.stat().st_size,
                     "sha256": sha256_file(target)})

    sums = "".join(f"{item['sha256']}  {item['name']}\n" for item in artifacts)
    (out_dir / "SHA256SUMS").write_text(sums, encoding="utf-8", newline="\n")

    commit = run_git(root, "rev-parse", "HEAD")
    dirty = bool(run_git(root, "status", "--porcelain"))
    manifest = {
        "schema": 1,
        "product": PRODUCT,
        "release": f"v{label}",
        "version": label,
        "source": {"commit": commit or "unknown", "dirty": dirty},
        "components": {
            "plugin": plugin_meta,
            "addon": addon_meta,
            "bridge": bridge_meta,
        },
        "bootstrap": pins,
        "artifacts": artifacts,
        "docs": docs,
    }
    (out_dir / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return {"out_dir": out_dir, "manifest": manifest}


def check_only(root: Path, label: str) -> dict:
    packager = load_addon_packager(root)
    _, plugin_meta = collect_plugin(root, label)
    _, addon_meta = collect_addon(root, packager)
    _, bridge_meta = collect_bridge(root, label, [])
    return {"ok": True, "version": label, "plugin": plugin_meta,
            "addon": addon_meta, "bridge": bridge_meta}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build reproducible WoWHermes release artifacts.")
    parser.add_argument("--version", required=True, help="release label, e.g. 0.2.0 (must match plugin.yaml)")
    parser.add_argument("--root", type=Path, default=None, help="repository root (default: parent of scripts/)")
    parser.add_argument("--out", type=Path, default=None, help="output root (default: <root>/dist)")
    parser.add_argument("--check-only", action="store_true", help="validate inputs, write nothing")
    parser.add_argument("--json", action="store_true", help="machine-readable result")
    args = parser.parse_args(argv)

    root = (args.root or repo_root()).resolve()
    out_root = (args.out or (root / "dist")).resolve()
    label = args.version.strip().lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", label):
        print(f"error: --version must look like 0.2.0 (got {args.version!r})", file=sys.stderr)
        return 2

    try:
        if args.check_only:
            result = check_only(root, label)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(f"OK: plugin {result['plugin']['version']}, addon {result['addon']['version']} "
                      f"(interface {result['addon']['interface']}), bridge {result['bridge']['version']}")
            return 0
        result = build(root, label, out_root)
    except ReleaseError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"ok": True, **result["manifest"]}, indent=2))
        return 0

    out_dir = result["out_dir"]
    manifest = result["manifest"]
    print(f"built {out_dir} (release v{manifest['version']}, commit {manifest['source']['commit'][:12]}"
          f"{' dirty' if manifest['source']['dirty'] else ''})")
    for item in manifest["artifacts"]:
        print(f"  {item['name']:<34} {item['bytes']:>9} bytes  {item['sha256']}  ({len(item['files'])} files)")
    print(f"  {'SHA256SUMS':<34} {len(manifest['artifacts'])} lines")
    print(f"  {'release-manifest.json':<34} plugin {manifest['components']['plugin']['version']}, "
          f"addon {manifest['components']['addon']['version']}, "
          f"bridge {manifest['components']['bridge']['version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
