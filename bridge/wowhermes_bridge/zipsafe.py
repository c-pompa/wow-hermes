"""Zip safety for third-party addon archives (research 08 §4).

Nothing here calls ``extractall``. Every entry is checked *before* a byte is written, the
write is ``open(..., "xb")`` after a containment check, and the extracted tree is walked
again afterwards. The rules, in order:

* archive budget — <= 32 MiB compressed, <= 96 MiB uncompressed, <= 2000 entries;
* no symlinks/hardlinks, no absolute paths, no drive letters, no ``..``, no NUL;
* Windows-reserved names and trailing dots/spaces rejected (NTFS aliases them);
* compression-ratio bombs: > 200:1 overall or per entry, or any entry over 16 MiB;
* extension allowlist (addon content only), plus an ``MZ`` / ``#!`` sniff on the bytes
  so a renamed executable is caught even when the extension lies;
* TOC layout and the Forever (`_Camelot` / `_Forever` / `_Mainline` / 16xxx interface)
  check that decides whether the archive is for this client at all.
"""

from __future__ import annotations

import re
import shutil
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

__all__ = [
    "ALLOWED_EXTENSIONS",
    "ZipRejected",
    "ExtractReport",
    "Layout",
    "extract",
    "read_layout",
    "classify_target",
]

MAX_COMPRESSED = 32 * 1024 * 1024
MAX_UNCOMPRESSED = 96 * 1024 * 1024
MAX_ENTRIES = 2000
MAX_ENTRY = 16 * 1024 * 1024
MAX_RATIO = 200

#: Addon content only. ``.exe .dll .bat .cmd .ps1 .vbs .js .scr .lnk .url .msi .py`` and
#: editor backups are deliberately absent.
ALLOWED_EXTENSIONS = frozenset(
    {
        ".toc",
        ".lua",
        ".xml",
        ".tga",
        ".blp",
        ".png",
        ".ogg",
        ".mp3",
        ".wav",
        ".ttf",
        ".otf",
        ".txt",
        ".md",
        ".textile",  # plain-text docs; Questie ships LibDataBroker README.textile
        ".json",
    }
)

_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {
    f"lpt{i}" for i in range(1, 10)
}
_DRIVE = re.compile(r"^[A-Za-z]:")


class ZipRejected(Exception):
    """The archive is not safe to unpack. ``reason`` is stable and machine-readable."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass
class ExtractReport:
    files: int = 0
    bytes: int = 0
    dirs: int = 0
    root: Path | None = None


@dataclass
class Layout:
    """What the extracted tree actually is."""

    folder: Path
    toc_name: str
    title: str = ""
    interfaces: list[int] = field(default_factory=list)
    flavor_tocs: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    optional_deps: list[str] = field(default_factory=list)
    required_deps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.folder.name


# ---------------------------------------------------------------- name checks


def _normalise(name: str) -> list[str]:
    """Split an archive entry into path parts, rejecting anything that can escape."""
    if "\x00" in name:
        raise ZipRejected("nul_in_name", name)
    raw = name.replace("\\", "/")
    if raw.startswith("/") or raw.startswith("//"):
        raise ZipRejected("absolute_path", name)
    if _DRIVE.match(raw):
        raise ZipRejected("drive_letter", name)
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    for part in parts:
        if part == "..":
            raise ZipRejected("path_traversal", name)
        if part != part.rstrip(" ."):
            raise ZipRejected("trailing_dot_or_space", name)
        stem = part.split(".", 1)[0].lower()
        if stem in _RESERVED:
            raise ZipRejected("reserved_name", name)
    return parts


def _check_extension(name: str) -> None:
    path = PurePosixPath(name.replace("\\", "/"))
    suffix = path.suffix.lower()
    # Common inert documentation files have no extension (Questie ships
    # Libs/Krowi_WorldMapButtons/LICENSE). Keep this list narrow rather than
    # allowing every extensionless payload.
    if not suffix and path.name.lower() in {"license", "copying", "notice", "readme"}:
        return
    if suffix not in ALLOWED_EXTENSIONS:
        raise ZipRejected("extension_not_allowed", name)


def _is_link(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode) or (info.external_attr & 0x400) != 0  # 0x400 = reparse point


# ---------------------------------------------------------------- extraction


def _write_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with zf.open(info, "r") as src, open(target, "xb") as dst:
            first = True
            while True:
                chunk = src.read(64 * 1024)
                if not chunk:
                    break
                if first:
                    first = False
                    head = chunk[:2]
                    if head == b"MZ":
                        raise ZipRejected("executable_content", info.filename)
                    if chunk[:2] == b"#!":
                        raise ZipRejected("script_content", info.filename)
                written += len(chunk)
                if written > MAX_ENTRY:
                    raise ZipRejected("entry_too_large", info.filename)
                dst.write(chunk)
    except BaseException:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    return written


def extract(zip_path: Path, dest: Path) -> ExtractReport:
    """Unpack ``zip_path`` into the empty directory ``dest`` with every rule applied."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    root = dest.resolve()

    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ZipRejected("not_a_zip", str(exc)) from exc

    report = ExtractReport(root=dest)
    seen: set[str] = set()
    total_uncompressed = 0
    total_compressed = 0

    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        dirs = [i for i in zf.infolist() if i.is_dir()]
        if len(infos) + len(dirs) > MAX_ENTRIES:
            raise ZipRejected("too_many_entries", str(len(infos) + len(dirs)))
        if not infos:
            raise ZipRejected("empty_archive")
        for info in zf.infolist():
            if info.is_dir():
                _normalise(info.filename)
                continue
            total_uncompressed += info.file_size
            total_compressed += max(info.compress_size, 0)

        if total_uncompressed > MAX_UNCOMPRESSED:
            raise ZipRejected("archive_too_large", f"{total_uncompressed} B uncompressed")
        if total_compressed > MAX_COMPRESSED:
            raise ZipRejected("archive_too_large", f"{total_compressed} B compressed")
        if total_compressed > 0 and total_uncompressed / total_compressed > MAX_RATIO:
            raise ZipRejected("compression_bomb", f"ratio {total_uncompressed / total_compressed:.0f}")

        for info in infos:
            parts = _normalise(info.filename)
            if not parts:
                continue
            if _is_link(info):
                raise ZipRejected("link_entry", info.filename)
            _check_extension(info.filename)
            key = "/".join(parts).lower()
            if key in seen:
                raise ZipRejected("case_collision", info.filename)
            seen.add(key)

            if info.file_size > MAX_ENTRY:
                raise ZipRejected("entry_too_large", info.filename)
            if info.compress_size > 0 and info.file_size / info.compress_size > MAX_RATIO:
                raise ZipRejected("entry_compression_bomb", info.filename)

            target = dest.joinpath(*parts)
            resolved_parent = target.parent.resolve()
            if root != resolved_parent and root not in resolved_parent.parents:
                raise ZipRejected("path_traversal", info.filename)
            report.bytes += _write_entry(zf, info, target)
            report.files += 1

    # Post-walk: nothing unlisted, nothing over budget (defence against a zip whose
    # central directory and local headers disagree).
    walked = 0
    for path in dest.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(dest)
        _check_extension(rel.as_posix())
        walked += path.stat().st_size
    if walked != report.bytes:
        shutil.rmtree(dest, ignore_errors=True)
        raise ZipRejected("size_mismatch", f"{walked} B on disk vs {report.bytes} B written")
    report.dirs = sum(1 for p in dest.rglob("*") if p.is_dir())
    return report


# ---------------------------------------------------------------- layout


def _parse_toc(text: str) -> tuple[str, list[int], dict[str, list[str]]]:
    title = ""
    interfaces: list[int] = []
    deps: dict[str, list[str]] = {"Dependencies": [], "OptionalDeps": [], "RequiredDeps": []}
    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line.startswith("##"):
            continue
        key, _, value = line[2:].partition(":")
        key = key.strip()
        value = value.strip()
        if key.lower() == "title" and not title:
            title = value
        elif key.lower() == "interface":
            interfaces.extend(int(n) for n in re.findall(r"\d{5,6}", value))
        elif key in deps:
            deps[key] = [p.strip() for p in re.split(r"[,\s]+", value) if p.strip()]
    return title, interfaces, deps


_FOREVER_TOKENS = ("_camelot", "_forever")
_MAINLINE_TOKENS = ("_mainline",)
_CLASSIC_TOKENS = ("_classic", "_vanilla", "_tbc", "_wrath", "_cata", "_mists", "_classiceraclassic")


def _toc_for(folder: Path) -> Path:
    plain = folder / f"{folder.name}.toc"
    if plain.is_file():
        return plain
    variants = sorted(p for p in folder.glob("*.toc") if p.is_file())
    if not variants:
        raise ZipRejected("no_toc", folder.name)
    # Prefer a Forever flavour TOC, then Mainline, then any.
    for token in _FOREVER_TOKENS + _MAINLINE_TOKENS:
        for candidate in variants:
            if candidate.stem.lower().endswith(token):
                return candidate
    return variants[0]


def read_layout(root: Path, *, folder: Path | None = None) -> Layout:
    """Locate the single addon folder in ``root`` and read its TOC.

    A zip whose TOC sits at the archive root is moved into a folder named after the TOC
    (research 08 §4.2: the classic "extra nested folder" failure).
    """
    root = Path(root)
    if folder is not None:
        return _layout_for(Path(folder))

    items = list(root.iterdir())
    dirs = [p for p in items if p.is_dir()]
    files = [p for p in items if p.is_file()]
    if len(dirs) == 1 and not files:
        return _layout_for(dirs[0])
    if not dirs and files:
        tocs = [p for p in files if p.suffix.lower() == ".toc"]
        if len(tocs) >= 1:
            holder = root / ".pkg"
            holder.mkdir(exist_ok=True)
            target = holder / tocs[0].stem
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            target.mkdir()
            for item in files:
                item.replace(target / item.name)
            return _layout_for(target)
    raise ZipRejected("bad_layout", "expected one addon folder or a single TOC at the root")


def _layout_for(folder: Path) -> Layout:
    toc = _toc_for(folder)
    try:
        text = toc.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ZipRejected("unreadable_toc", str(exc)) from exc
    title, interfaces, deps = _parse_toc(text)
    flavor_tocs = sorted(p.name for p in folder.glob("*.toc") if p.is_file())
    layout = Layout(
        folder=folder,
        toc_name=toc.name,
        title=title,
        interfaces=interfaces,
        flavor_tocs=flavor_tocs,
        dependencies=deps["Dependencies"],
        optional_deps=deps["OptionalDeps"],
        required_deps=deps["RequiredDeps"],
    )
    return layout


def classify_target(layout: Layout) -> str:
    """``forever`` | ``classic`` | ``other`` | ``unknown`` for this client (Interface 16001).

    A ``_Camelot.toc`` / ``_Forever.toc`` or any 16000..16999 interface means Forever.
    ``_Classic*`` TOCs with no Forever marker are Classic-only: the client will not load
    them, so the caller refuses. Anything else is reported, not hidden.
    """
    stems = [Path(name).stem.lower() for name in layout.flavor_tocs]
    has_forever = any(stem.endswith(token) for stem in stems for token in _FOREVER_TOKENS)
    has_classic = any(stem.endswith(token) for stem in stems for token in _CLASSIC_TOKENS)
    has_mainline = any(stem.endswith(token) for stem in stems for token in _MAINLINE_TOKENS)
    forever_iface = any(16000 <= value <= 16999 for value in layout.interfaces)

    if has_forever or forever_iface:
        return "forever"
    if has_classic and not has_mainline:
        return "classic"
    if layout.interfaces or has_mainline:
        return "other"
    return "unknown"
