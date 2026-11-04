"""Adapter: read font files out of zip / 7z / rar archives through one interface.

zip — stdlib; 7z — py7zr; rar — rarfile, which needs an external tool (unrar, unar, bsdtar or 7z).
An unavailable format raises ArchiveUnsupported with a message meant for `subtitles.json` warnings.
"""

from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from istok.subs.names import FONT_EXTS

MAX_MEMBER_BYTES = 50 * 1024 * 1024


class ArchiveUnsupported(Exception):
    pass


def archive_type(name: str) -> str | None:
    ext = PurePosixPath(name).suffix.lower()
    return {".zip": "zip", ".7z": "7z", ".rar": "rar"}.get(ext)


def _is_font(member: str) -> bool:
    return PurePosixPath(member).suffix.lower() in FONT_EXTS


def extract_fonts(name: str, data: bytes) -> list[tuple[str, bytes]]:
    """[(member path, bytes)] of the font files inside an archive."""
    kind = archive_type(name)
    try:
        if kind == "zip":
            return _zip(data)
        if kind == "7z":
            return _7z(data)
        if kind == "rar":
            return _rar(data)
    except ArchiveUnsupported:
        raise
    except Exception as e:  # noqa: BLE001 — corrupt archives come in many flavours
        raise ArchiveUnsupported(f"{name}: cannot read archive ({e})") from e
    raise ArchiveUnsupported(f"{name}: not an archive")


def _zip(data: bytes) -> list[tuple[str, bytes]]:
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if not info.is_dir() and _is_font(info.filename) and info.file_size <= MAX_MEMBER_BYTES:
                out.append((info.filename, z.read(info)))
    return out


def _7z(data: bytes) -> list[tuple[str, bytes]]:
    try:
        import py7zr
    except ImportError as e:
        raise ArchiveUnsupported("7z support (py7zr) is not installed") from e
    out = []
    with tempfile.TemporaryDirectory(prefix="stremio-7z-") as tmp, py7zr.SevenZipFile(io.BytesIO(data)) as z:
        targets = [n for n in z.getnames() if _is_font(n)]
        if targets:
            z.extract(path=tmp, targets=targets)
        out = _collect(Path(tmp))
    return out


def _rar(data: bytes) -> list[tuple[str, bytes]]:
    import rarfile

    try:
        rarfile.tool_setup()
    except rarfile.RarCannotExec as e:
        raise ArchiveUnsupported("RAR needs unrar, unar, bsdtar or 7z installed") from e
    out = []
    with rarfile.RarFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if not info.is_dir() and _is_font(info.filename) and info.file_size <= MAX_MEMBER_BYTES:
                out.append((info.filename, z.read(info)))
    return out


def _collect(root: Path) -> list[tuple[str, bytes]]:
    out = []
    resolved = root.resolve()
    for p in sorted(root.rglob("*")):
        rp = p.resolve()
        if p.is_file() and not p.is_symlink() and rp.is_relative_to(resolved) and _is_font(p.name):
            if p.stat().st_size <= MAX_MEMBER_BYTES:
                out.append((str(p.relative_to(root)), p.read_bytes()))
    return out
