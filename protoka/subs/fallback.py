"""Step 5c: substitutes for fonts a script needs but the torrent does not have.

Why the script is rewritten: libass (mpv, VLC, mpv-android…) picks an attached font by its family name only.
Attaching "Noto Sans" does nothing for a style that asks for "Foo Sans": the style must name "Noto Sans".

    missing name ──letterform.classify──▶ Letterform (category, cjk, bold, italic, width, twin)
    what it draws ──ass.chars_by_font──▶ characters per (bold, italic)
    FallbackLibrary.choose ──▶ family:  1. the metric twin (Arial → Liberation Sans) if it covers the text
                                        2. the first family of the category that covers every character
                                           (CJK families first when the name or the text is CJK)
                                        3. else the family covering the most characters
                               faces:   per (bold, italic) used, the face with the nearest weight/slant
    subset ──▶ only the glyphs actually drawn (a 20 MB CJK font becomes a few KB)

The library indexes a font directory (the image ships Noto, Noto CJK and Liberation in /usr/share/fonts)
but only the families listed in FAMILIES: a host's other fonts never leak into the stream.
"""

from __future__ import annotations

import functools
import hashlib
import io
import logging
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

from fontTools.subset import Options, Subsetter
from fontTools.ttLib import TTCollection, TTFont, TTLibError

from protoka.subs import ass, fonts, letterform
from protoka.subs.letterform import CASUAL, MONO, SANS, SERIF, Letterform
from protoka.subs.model import FontFace

log = logging.getLogger(__name__)

# family → how it is used. Order inside a category = preference.
LATIN: dict[str, list[str]] = {
    SANS: ["Noto Sans", "Liberation Sans"],
    SERIF: ["Noto Serif", "Liberation Serif"],
    MONO: ["Noto Sans Mono", "Liberation Mono"],
    CASUAL: ["Noto Sans", "Liberation Sans"],  # nothing handwritten is bundled
}
CJK: dict[str, list[str]] = {
    SANS: ["Noto Sans CJK JP"],
    SERIF: ["Noto Serif CJK JP"],
    MONO: ["Noto Sans CJK JP"],
    CASUAL: ["Noto Sans CJK JP"],
}
FAMILIES = frozenset(f for d in (LATIN, CJK) for fams in d.values() for f in fams)
FONT_EXTS = (".ttf", ".otf", ".ttc", ".otc")


@dataclass(frozen=True)
class Face:
    path: str
    number: int  # face index inside a collection (.ttc), else 0
    family: str
    weight: int  # OS/2 usWeightClass
    italic: bool
    is_cff: bool  # .otf outlines
    cmap: frozenset[int] = field(repr=False)

    @property
    def style(self) -> str:
        return ("Bold" if self.weight >= 600 else "Regular") + (" Italic" if self.italic else "")


@dataclass(frozen=True)
class Substitution:
    """One replaced font, explained (for /subtitles.json and the log)."""

    missing: str
    family: str
    faces: tuple[str, ...]  # "Bold", "Regular Italic"…
    chars: int
    uncovered: int
    reasons: tuple[str, ...]
    substitute: ass.Substitute

    def describe(self) -> str:
        cover = f"all {self.chars} chars covered" if not self.uncovered else (
            f"{self.uncovered} of {self.chars} chars not covered"
        )  # fmt: skip
        extra = f", width ×{self.substitute.scale_x:g}" if self.substitute.scale_x != 1 else ""
        why = "; ".join((*self.reasons, cover))
        return f'"{self.missing}" → "{self.family}" {"/".join(self.faces)}{extra} ({why})'

    def as_json(self) -> dict[str, object]:
        return {
            "font": self.missing,
            "substitute": self.family,
            "faces": list(self.faces),
            "scaleX": self.substitute.scale_x,
            "chars": self.chars,
            "uncovered": self.uncovered,
            "reasons": list(self.reasons),
        }


@dataclass
class Plan:
    substitutes: dict[str, ass.Substitute] = field(default_factory=dict)
    faces: list[FontFace] = field(default_factory=list)
    substitutions: list[Substitution] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)  # nothing installed could stand in


def _read_face(path: Path, number: int, font: TTFont) -> Face | None:
    name = font["name"]
    family = name.getDebugName(16) or name.getDebugName(1)
    if family not in FAMILIES:
        return None
    os2 = font["OS/2"] if "OS/2" in font else None
    weight = int(os2.usWeightClass) if os2 else 400
    italic = bool(os2 and os2.fsSelection & 1) or bool(font["head"].macStyle & 2)
    cmap = frozenset(font.getBestCmap() or {})
    return Face(str(path), number, family, weight, italic, "CFF " in font or "CFF2" in font, cmap)


def scan(directory: Path) -> list[Face]:
    """Faces of the bundled families found under `directory` (recursively). Blocking."""
    out: list[Face] = []
    if not directory.is_dir():
        return out
    for path in sorted(directory.rglob("*")):
        if path.suffix.lower() not in FONT_EXTS or not path.is_file():
            continue
        try:
            if path.suffix.lower() in (".ttc", ".otc"):
                coll = TTCollection(str(path), lazy=True)
                fonts_ = list(enumerate(coll.fonts))
            else:
                fonts_ = [(0, TTFont(str(path), lazy=True))]
            for number, font in fonts_:
                face = _read_face(path, number, font)
                if face is not None:
                    out.append(face)
        except (TTLibError, OSError, KeyError, AssertionError) as e:
            log.debug("fallback fonts: cannot read %s: %s", path, e)
    return out


class FallbackLibrary:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self._faces: list[Face] | None = None
        self._lock = threading.Lock()

    @property
    def faces(self) -> list[Face]:
        with self._lock:  # scanned once, by whichever worker thread asks first
            if self._faces is None:
                self._faces = scan(self.directory)
                fams = sorted({f.family for f in self._faces})
                log.info("fallback fonts: %s in %s", ", ".join(fams) or "none", self.directory)
            return self._faces

    def families(self) -> dict[str, list[Face]]:
        out: dict[str, list[Face]] = {}
        for f in self.faces:
            out.setdefault(f.family, []).append(f)
        return out

    # -- choosing ----------------------------------------------------------------------------------
    def plan(self, missing: set[str], scripts: list[str]) -> Plan:
        """Substitutes for every missing font the ASS `scripts` draw with. Blocking (fontTools).

        Two passes: the family is chosen from what the missing font draws in the original scripts; the faces
        (Regular/Bold/Italic) from the rewritten ones, where a bold-by-name font has become a bold style
        or `\\b1` — and that bold carries over to whatever follows in the same line, like in the player."""
        usage = merged_usage(scripts)
        plan = Plan()
        fams = self.families()
        for name in sorted(missing):
            variants = usage.get(name)
            if not variants:
                continue  # declared by a style nobody uses
            lf = letterform.classify(name)
            chosen = _choose(lf, variants, fams)
            if chosen is None:
                plan.unresolved.append(name)
                continue
            family, reasons, uncovered, chars = chosen
            twin = family == lf.twin
            sub = ass.Substitute(family, lf.bold, lf.italic, 1.0 if twin else lf.width)
            plan.substitutes[name] = sub
            plan.substitutions.append(
                Substitution(name, family, (), chars, uncovered, (*lf.reasons, *reasons), sub)
            )
        if not plan.substitutes:
            return plan
        drawn = merged_usage([ass.rename_fonts(s, plan.substitutes) for s in scripts])
        styles_of: dict[str, tuple[str, ...]] = {}
        for family in sorted({s.family for s in plan.substitutes.values()}):
            used: dict[Face, set[str]] = {}
            for (bold, italic), cs in drawn.get(family.lower(), {}).items():
                used.setdefault(_nearest(fams[family], bold, italic), set()).update(cs)
            for face, cs in sorted(used.items(), key=lambda kv: kv[0].style):
                plan.faces.append(_subset_face(face, frozenset(cs)))
            styles_of[family] = tuple(sorted(f.style for f in used))
        plan.substitutions = [replace(s, faces=styles_of.get(s.family, ())) for s in plan.substitutions]
        return plan


def merged_usage(scripts: list[str]) -> dict[str, dict[tuple[bool, bool], set[str]]]:
    """font (lowercase) → (bold, italic) → characters it draws, over several scripts."""
    out: dict[str, dict[tuple[bool, bool], set[str]]] = {}
    for script in scripts:
        for font, variants in ass.chars_by_font(script).items():
            for key, chars in variants.items():
                out.setdefault(font, {}).setdefault(key, set()).update(chars)
    return out


def _choose(
    lf: Letterform, variants: dict[tuple[bool, bool], set[str]], fams: dict[str, list[Face]]
) -> tuple[str, tuple[str, ...], int, int] | None:
    chars = {ord(c) for cs in variants.values() for c in cs if not c.isspace()}
    text_cjk = letterform.has_cjk({chr(c) for c in chars})
    order: list[str] = []
    if lf.twin:
        order.append(lf.twin)
    cjk_first = lf.cjk or text_cjk
    groups = [CJK[lf.category], LATIN[lf.category]] if cjk_first else [LATIN[lf.category], CJK[lf.category]]
    for g in groups:
        order += [f for f in g if f not in order]
    for fam in [f for c in (SANS, SERIF, MONO) for d in (LATIN, CJK) for f in d[c]]:
        if fam not in order:
            order.append(fam)  # any other bundled family, as the very last resort
    reasons: list[str] = []
    if lf.category == CASUAL:
        reasons.append("no handwritten face is bundled: using sans")
    best: tuple[int, str] | None = None
    for fam in order:
        faces = fams.get(fam)
        if not faces:
            if fam == lf.twin:
                reasons.append(f"{fam} is not installed")
            continue
        covered = set().union(*(f.cmap for f in faces))
        missing = len(chars - covered)
        if missing == 0:
            if fam in CJK[lf.category] and text_cjk and not lf.cjk:
                reasons.append("cjk: the text has CJK characters")
            return fam, tuple(reasons), 0, len(chars)
        if fam == lf.twin:
            reasons.append(f"{fam} lacks {missing} of the characters")
        if best is None or missing < best[0]:
            best = (missing, fam)
    if best is None:
        return None
    return best[1], (*reasons, "no bundled family covers every character"), best[0], len(chars)


def _nearest(faces: list[Face], bold: bool, italic: bool) -> Face:
    want = 700 if bold else 400
    return min(faces, key=lambda f: (f.italic != italic, abs(f.weight - want), f.path, f.number))


@functools.lru_cache(maxsize=64)
def _subset_bytes(path: str, number: int, chars: frozenset[str]) -> bytes:
    font = TTFont(path, fontNumber=number, recalcTimestamp=False, lazy=False)
    opts = Options()
    opts.name_IDs = ["*"]
    opts.name_languages = ["*"]
    opts.name_legacy = True
    opts.layout_features = ["*"]
    opts.notdef_outline = True
    opts.hinting = False
    opts.drop_tables += ["FFTM"]
    sub = Subsetter(opts)
    sub.populate(text="".join(sorted(chars)))
    sub.subset(font)
    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue()


ALWAYS = frozenset(" \u00a0")  # spaces are not "drawn", but a subset without them sends libass elsewhere


def _subset_face(face: Face, chars: frozenset[str]) -> FontFace:
    data = _subset_bytes(face.path, face.number, chars | ALWAYS)
    digest = hashlib.sha1("".join(sorted(chars)).encode(), usedforsecurity=False).hexdigest()[:8]
    stem = f"{face.family.replace(' ', '')}-{face.style.replace(' ', '')}"
    filename = f"{stem}.subset-{digest}{'.otf' if face.is_cff else '.ttf'}"
    return fonts.make_face(f"fallback/{filename}", filename, data)


_libraries: dict[str, FallbackLibrary] = {}


def library(directory: str | Path) -> FallbackLibrary:
    """One shared index per directory (scanning Noto CJK takes a moment: do it once per process)."""
    key = str(directory)
    if key not in _libraries:
        _libraries[key] = FallbackLibrary(Path(directory))
    return _libraries[key]
