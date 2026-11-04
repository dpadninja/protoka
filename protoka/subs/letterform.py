"""Step 5b: what a missing font looks like, guessed from its name. Pure.

We never have the missing font itself, only the name a script asks for ("Times New Roman", "Foo Serif
Bold", "MS Mincho", "Impact"). From it we infer the letterform class a substitute should have:

    category   sans | serif | mono | casual        (CJK is decided by the glyphs to draw, see fallback.py)
    cjk        the name is a Japanese/Chinese/Korean font (MS Gothic, SimSun, a name in kanji)
    bold       the name says so ("Arial Black", "… Bold"); the script's own \\b flags apply on top
    italic     the name says so
    width      1.0, or narrower/wider than the substitute ("Impact", "… Condensed")
    twin       a metric-compatible substitute (Arial → Liberation Sans): same advance of every glyph,
               so positioned signs keep their layout; no width correction is then needed

Rules run in order (most specific first) and every one that decides something leaves a reason, so the
choice can be explained in /subtitles.json and in the log.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace

SANS, SERIF, MONO, CASUAL = "sans", "serif", "mono", "casual"
NARROW, WIDE = 0.82, 1.12


@dataclass(frozen=True)
class Letterform:
    category: str = SANS
    cjk: bool = False
    bold: bool = False
    italic: bool = False
    width: float = 1.0
    twin: str | None = None
    reasons: tuple[str, ...] = field(default=())

    def because(self, reason: str, **changes: object) -> Letterform:
        return replace(self, reasons=(*self.reasons, reason), **changes)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Known:
    category: str
    twin: str | None = None
    width: float = 1.0
    bold: bool = False
    cjk: bool = False


# Fonts that old releases name most often. Twins: Liberation is metric-compatible with these MS core fonts.
KNOWN: dict[str, Known] = {
    # metric twins
    "arial": Known(SANS, "Liberation Sans"),
    "helvetica": Known(SANS, "Liberation Sans"),
    "arial unicode ms": Known(SANS, "Liberation Sans"),
    "times new roman": Known(SERIF, "Liberation Serif"),
    "times": Known(SERIF, "Liberation Serif"),
    "courier new": Known(MONO, "Liberation Mono"),
    "courier": Known(MONO, "Liberation Mono"),
    # sans
    "verdana": Known(SANS),
    "tahoma": Known(SANS),
    "trebuchet ms": Known(SANS),
    "segoe ui": Known(SANS),
    "calibri": Known(SANS),
    "candara": Known(SANS),
    "corbel": Known(SANS),
    "franklin gothic medium": Known(SANS),
    "gill sans mt": Known(SANS),
    "century gothic": Known(SANS),
    "lucida sans unicode": Known(SANS),
    "open sans": Known(SANS),
    "roboto": Known(SANS),
    "dejavu sans": Known(SANS),
    "myriad pro": Known(SANS),
    "futura": Known(SANS),
    "arial black": Known(SANS, bold=True),
    "arial narrow": Known(SANS, width=NARROW),
    "impact": Known(SANS, width=NARROW, bold=True),
    "haettenschweiler": Known(SANS, width=0.7, bold=True),
    "agency fb": Known(SANS, width=0.75),
    "bahnschrift condensed": Known(SANS, width=NARROW),
    "franklin gothic demi cond": Known(SANS, width=NARROW, bold=True),
    # serif
    "georgia": Known(SERIF),
    "cambria": Known(SERIF),
    "constantia": Known(SERIF),
    "garamond": Known(SERIF),
    "book antiqua": Known(SERIF),
    "palatino linotype": Known(SERIF),
    "century schoolbook": Known(SERIF),
    "bookman old style": Known(SERIF),
    "baskerville old face": Known(SERIF),
    "dejavu serif": Known(SERIF),
    # mono
    "consolas": Known(MONO),
    "lucida console": Known(MONO),
    "dejavu sans mono": Known(MONO),
    # casual / handwriting: nothing like them is bundled, the reason says so
    "comic sans ms": Known(CASUAL),
    "segoe print": Known(CASUAL),
    "segoe script": Known(CASUAL),
    "monotype corsiva": Known(CASUAL),
    "brush script mt": Known(CASUAL),
    "kristen itc": Known(CASUAL),
    "mistral": Known(CASUAL),
    "papyrus": Known(CASUAL),
    # CJK
    "ms gothic": Known(SANS, cjk=True),
    "ms pgothic": Known(SANS, cjk=True),
    "ms ui gothic": Known(SANS, cjk=True),
    "meiryo": Known(SANS, cjk=True),
    "meiryo ui": Known(SANS, cjk=True),
    "yu gothic": Known(SANS, cjk=True),
    "ms mincho": Known(SERIF, cjk=True),
    "ms pmincho": Known(SERIF, cjk=True),
    "yu mincho": Known(SERIF, cjk=True),
    "simhei": Known(SANS, cjk=True),
    "microsoft yahei": Known(SANS, cjk=True),
    "simsun": Known(SERIF, cjk=True),
    "nsimsun": Known(SERIF, cjk=True),
    "mingliu": Known(SERIF, cjk=True),
    "pmingliu": Known(SERIF, cjk=True),
    "microsoft jhenghei": Known(SANS, cjk=True),
    "malgun gothic": Known(SANS, cjk=True),
    "batang": Known(SERIF, cjk=True),
    "gulim": Known(SANS, cjk=True),
    "dfkai-sb": Known(SERIF, cjk=True),
}

_WORD_RE = re.compile(r"[a-z0-9]+")
_STYLE_WORDS = {
    "bold", "black", "heavy", "extrabold", "ultrabold", "semibold", "demibold", "demi", "bd", "blk",
    "italic", "oblique", "it", "regular", "normal", "book", "medium", "light", "thin", "condensed",
    "narrow", "compressed", "cond", "cn", "extended", "expanded", "wide", "std", "pro", "mt",
}  # fmt: skip
_BOLD = {"bold", "black", "heavy", "extrabold", "ultrabold", "semibold", "demibold", "bd", "blk"}
_ITALIC = {"italic", "oblique", "it"}
_NARROW = {"condensed", "narrow", "compressed", "cond", "cn"}
_WIDE = {"extended", "expanded", "wide"}
_SERIF = {"serif", "roman", "antiqua", "mincho", "ming", "song", "sung", "batang", "myeongjo", "garamond"}
_SANS = {"sans", "gothic", "grotesk", "grotesque", "hei", "gulim", "dotum", "goshikku"}
_MONO = {"mono", "monospace", "code", "console", "courier", "typewriter", "terminal"}
_CASUAL = {"script", "hand", "handwriting", "brush", "comic", "marker", "print", "calligraphy", "kaisho"}


def _words(name: str) -> list[str]:
    # "FooSerifBold" → "foo serif bold": split camel case before lowering
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return _WORD_RE.findall(spaced.lower())


def _is_cjk_char(c: str) -> bool:
    try:
        n = unicodedata.name(c)
    except ValueError:
        return False
    return n.startswith(("CJK ", "HIRAGANA", "KATAKANA", "HANGUL"))


def classify(name: str) -> Letterform:
    """Letterform of the font a script names but nobody provides."""
    lf = Letterform()
    clean = name.strip().lstrip("@").strip()
    words = _words(clean)
    family_words = [w for w in words if w not in _STYLE_WORDS]
    known = KNOWN.get(" ".join(words)) or KNOWN.get(" ".join(family_words))
    if known is not None:
        lf = lf.because(
            f"{known.category}: {clean!r} is a known {known.category} font",
            category=known.category,
            cjk=known.cjk,
            twin=known.twin,
        )
        if known.twin:
            lf = lf.because(f"metric twin: {known.twin} has the same glyph widths")
        if known.width != 1.0:
            lf = lf.because(f"width ×{known.width:g}: {clean!r} is narrower", width=known.width)
        if known.bold:
            lf = lf.because(f"bold: {clean!r} is a bold face", bold=True)
        if known.cjk:
            lf = lf.because("cjk: a Japanese/Chinese/Korean font")
    else:
        ws = set(words)
        if ws & _MONO:
            lf = lf.because(f"mono: name says {sorted(ws & _MONO)[0]!r}", category=MONO)
        elif ws & _CASUAL:
            lf = lf.because(f"casual: name says {sorted(ws & _CASUAL)[0]!r}", category=CASUAL)
        elif ws & _SERIF and not ws & {"sans"}:
            lf = lf.because(f"serif: name says {sorted(ws & _SERIF)[0]!r}", category=SERIF)
        elif ws & _SANS:
            lf = lf.because(f"sans: name says {sorted(ws & _SANS)[0]!r}", category=SANS)
        else:
            lf = lf.because("sans: nothing in the name suggests otherwise (most subtitle fonts are sans)")
        if any(_is_cjk_char(c) for c in clean):
            lf = lf.because("cjk: the name is written in CJK characters", cjk=True)
    ws = set(words)
    if ws & _BOLD and not lf.bold:
        lf = lf.because(f"bold: name says {sorted(ws & _BOLD)[0]!r}", bold=True)
    if ws & _ITALIC:
        lf = lf.because(f"italic: name says {sorted(ws & _ITALIC)[0]!r}", italic=True)
    if lf.width == 1.0 and ws & _NARROW:
        lf = lf.because(f"width ×{NARROW:g}: name says {sorted(ws & _NARROW)[0]!r}", width=NARROW)
    elif lf.width == 1.0 and ws & _WIDE:
        lf = lf.because(f"width ×{WIDE:g}: name says {sorted(ws & _WIDE)[0]!r}", width=WIDE)
    return lf


def has_cjk(chars: set[str]) -> bool:
    return any(_is_cjk_char(c) for c in chars)
