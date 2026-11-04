"""Steps 3 and 5: where the fonts are, what families they contain, which ones a script needs. Pure.

Fonts in old releases: loose files in `Fonts/` (or `[Fonts]`, `Шрифты/`) next to the videos, or one
archive (`Fonts.rar`, `fonts.7z`, `Шрифты.zip`). A font file's *name* says little about its family,
so the family names come from the font itself (OpenType `name` table).
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterable, Sequence
from pathlib import PurePosixPath

from fontTools.ttLib import TTCollection, TTFont

from protoka.subs.model import FileName, FontFace, FontSource, Kind
from protoka.subs.names import FONT_FOLDER_WORDS, folder_tokens

log = logging.getLogger(__name__)

# name table IDs: 1 family, 4 full name, 6 PostScript name, 16 typographic family
_NAME_IDS = (1, 4, 6, 16)


def _in_font_folder(f: FileName) -> bool:
    return any(set(folder_tokens(folder)) & FONT_FOLDER_WORDS for folder in f.folders)


def _is_font_archive(f: FileName) -> bool:
    if f.kind is not Kind.ARCHIVE:
        return False
    name_tokens = set(folder_tokens(f.stem))
    return bool(name_tokens & FONT_FOLDER_WORDS) or _in_font_folder(f) or "font" in f.stem.lower()


def _shared_depth(a: Sequence[str], b: Sequence[str]) -> int:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


def locate(files: Sequence[FileName], video: FileName) -> list[FontSource]:
    """Font files and font archives, the ones closest to the video first."""
    found = [f for f in files if f.kind is Kind.FONT or _is_font_archive(f)]
    found.sort(key=lambda f: (-_shared_depth(f.folders, video.folders), f.kind is Kind.ARCHIVE, f.path))
    return [
        FontSource(
            id=str(f.index), file_idx=f.index, path=f.path, is_archive=f.kind is Kind.ARCHIVE, size=f.size
        )
        for f in found
    ]


def mime_type(filename: str) -> str:
    """MIME types understood by mpv/libass, ffmpeg and VLC for Matroska font attachments."""
    ext = PurePosixPath(filename).suffix.lower()
    if ext in (".otf", ".otc"):
        return "application/vnd.ms-opentype"
    return "application/x-truetype-font"


def families(data: bytes) -> frozenset[str]:
    """All family / full / PostScript names (lowercase) of a font or font collection."""
    names: set[str] = set()
    try:
        if data[:4] == b"ttcf":
            fonts = TTCollection(io.BytesIO(data), lazy=True).fonts
        else:
            fonts = [TTFont(io.BytesIO(data), lazy=True, fontNumber=0)]
        for font in fonts:
            for rec in font["name"].names:
                if rec.nameID in _NAME_IDS:
                    try:
                        value = rec.toUnicode()
                    except UnicodeDecodeError:
                        continue
                    if value.strip():
                        names.add(value.strip().lower())
    except Exception as e:  # noqa: BLE001 — broken fonts are common in old releases
        log.debug("cannot read font: %s", e)
    return frozenset(names)


def make_face(face_id: str, filename: str, data: bytes) -> FontFace:
    return FontFace(
        id=face_id,
        filename=PurePosixPath(filename).name,
        data=data,
        families=families(data),
        mime=mime_type(filename),
    )


def pick(needed: Iterable[str], faces: Iterable[FontFace]) -> tuple[list[FontFace], set[str]]:
    """Faces providing any needed name (case-insensitive), and the names nobody provides."""
    wanted = {n.lower() for n in needed}
    picked: list[FontFace] = []
    covered: set[str] = set()
    seen_ids: set[str] = set()
    for face in faces:
        hit = wanted & face.families
        if hit and face.id not in seen_ids:
            picked.append(face)
            seen_ids.add(face.id)
            covered |= hit
    return picked, wanted - covered
