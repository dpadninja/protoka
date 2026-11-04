"""Font fallback: letterform from the name, coverage-driven choice, script rewriting, subsets (all offline)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

from istok.subs import ass, fallback, letterform
from istok.subs.letterform import MONO, NARROW, SANS, SERIF
from tests.media import KANJI, ass_script, write_fallback_fonts


@pytest.fixture
def bundle(tmp_path: Path) -> fallback.FallbackLibrary:
    return fallback.FallbackLibrary(write_fallback_fonts(tmp_path / "fonts"))


# -- letterform from the name ----------------------------------------------------------------------
LETTERFORMS = [
    # name,                   category, cjk,   bold,  italic, width,  twin
    ("Arial", SANS, False, False, False, 1.0, "Liberation Sans"),
    ("Times New Roman", SERIF, False, False, False, 1.0, "Liberation Serif"),
    ("Courier New", MONO, False, False, False, 1.0, "Liberation Mono"),
    ("Arial Bold", SANS, False, True, False, 1.0, "Liberation Sans"),
    ("Impact", SANS, False, True, False, NARROW, None),
    ("Arial Narrow", SANS, False, False, False, NARROW, None),
    ("Foo Serif Bold", SERIF, False, True, False, 1.0, None),
    ("FooSerifItalic", SERIF, False, False, True, 1.0, None),
    ("Bar Sans Condensed", SANS, False, False, False, NARROW, None),
    ("Baz Mono", MONO, False, False, False, 1.0, None),
    ("Georgia", SERIF, False, False, False, 1.0, None),
    ("MS Mincho", SERIF, True, False, False, 1.0, None),
    ("MS Gothic", SANS, True, False, False, 1.0, None),
    ("@MS Gothic", SANS, True, False, False, 1.0, None),  # vertical writing
    ("ＭＳ 明朝", SANS, True, False, False, 1.0, None),  # name in CJK characters
    ("Whatever Display", SANS, False, False, False, 1.0, None),  # nothing to go on: sans
]


@pytest.mark.parametrize(("name", "category", "cjk", "bold", "italic", "width", "twin"), LETTERFORMS)
def test_letterform_from_the_name(name, category, cjk, bold, italic, width, twin):
    lf = letterform.classify(name)
    assert (lf.category, lf.cjk, lf.bold, lf.italic, lf.width, lf.twin) == (
        category,
        cjk,
        bold,
        italic,
        width,
        twin,
    )
    assert lf.reasons  # every decision is explained


# -- what each font draws, and rewriting the script ------------------------------------------------
SCRIPT = ass_script(
    [
        ("0:00:01.00", "0:00:02.00", r"Привет {\bord2\b1}мир{\fnImpact}SIGN{\fnTest Sans}ok"),
        ("0:00:02.00", "0:00:03.00", r"{\pos(1,2)\fscx120\fnImpact}AB{\p1}m 0 0 l 9 9{\p0}{\r}x"),
    ],
    font="Arial",
)


def test_chars_by_font_follows_overrides_and_skips_drawings():
    usage = ass.chars_by_font(SCRIPT)
    assert usage["arial"] == {(False, False): set("Привет") | {"x"}, (True, False): set("мир")}
    assert usage["impact"] == {(True, False): set("SIGN"), (False, False): set("AB")}  # no "m 0 0 l 9 9"
    assert usage["test sans"] == {(True, False): set("ok")}


def test_rename_fonts_rewrites_styles_and_overrides_only():
    subs = {
        "arial": ass.Substitute("Liberation Sans"),
        "impact": ass.Substitute("Noto Sans", bold=True, scale_x=0.82),
    }
    out = ass.rename_fonts(SCRIPT, subs)
    assert "Style: Default,Liberation Sans,20," in out
    assert r"мир{\fnNoto Sans\b1\fscx82}SIGN{\fnTest Sans\fscx100}ok" in out  # width restored on leaving
    assert r"{\pos(1,2)\fscx120\fnNoto Sans\b1\fscx98.4}AB" in out  # the author's \fscx120, narrowed
    assert r"{\bord2\b1}" in out  # \bord is not \b
    # nothing else changed
    assert (
        out.replace("Liberation Sans", "Arial")
        .replace(r"\fnNoto Sans\b1\fscx82", r"\fnImpact")
        .replace(r"\fnTest Sans\fscx100", r"\fnTest Sans")
        .replace(r"\fnNoto Sans\b1\fscx98.4", r"\fnImpact")
        == SCRIPT
    )


def test_rename_keeps_vertical_writing_and_scales_the_style():
    script = ass_script([("0:00:01.00", "0:00:02.00", r"{\fn@Impact}縦")], font="@Impact")
    out = ass.rename_fonts(script, {"impact": ass.Substitute("Noto Sans CJK JP", scale_x=0.5)})
    assert (
        "Style: Default,@Noto Sans CJK JP,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,50,100"
        in out
    )
    assert r"{\fn@Noto Sans CJK JP}縦" in out  # same ratio as the style: no extra \fscx


# -- choosing a substitute ------------------------------------------------------------------------
def drawn(font: str, text: str) -> str:
    return ass_script([("0:00:01.00", "0:00:02.00", text)], font=font)


def test_bold_by_name_carries_over_to_what_follows_in_the_line(bundle):
    # "Foo Serif Bold" becomes a bold style, so the MS Mincho run after \fn is drawn bold too (a real libass
    # run picked a system bold face otherwise): the faces are chosen from the rewritten script
    script = drawn("Foo Serif Bold", r"Привет {\fnMS Mincho}日本語")
    plan = bundle.plan({"foo serif bold", "ms mincho"}, [script])
    by = {s.missing: s for s in plan.substitutions}
    assert by["foo serif bold"].faces == ("Bold",)
    assert by["ms mincho"].family == "Noto Serif CJK JP"
    assert by["ms mincho"].faces == ("Regular",)  # wanted bold; the nearest bundled face is Regular


def test_metric_twin_is_preferred(bundle):
    plan = bundle.plan({"arial"}, [drawn("Arial", "Hello Привет")])
    (s,) = plan.substitutions
    assert s.family == "Liberation Sans" and s.substitute.scale_x == 1.0 and s.uncovered == 0
    assert any("metric twin" in r for r in s.reasons)


def test_twin_without_the_characters_gives_way(bundle):
    # the fake Liberation Serif has no Cyrillic: Noto Serif draws the Russian text instead
    plan = bundle.plan({"times new roman"}, [drawn("Times New Roman", "Текст")])
    (s,) = plan.substitutions
    assert s.family == "Noto Serif"
    assert "Liberation Serif lacks 5 of the characters" in s.reasons


def test_letterform_and_style_pick_family_and_faces(bundle):
    plan = bundle.plan({"foo serif"}, [drawn("Foo Serif", r"Ab{\i1}cd{\i0\b1}Ж")])
    (s,) = plan.substitutions
    assert s.family == "Noto Serif" and s.faces == ("Bold", "Regular", "Regular Italic")
    assert sorted(f.filename.split(".")[0] for f in plan.faces) == [
        "NotoSerif-Bold", "NotoSerif-Regular", "NotoSerif-RegularItalic",
    ]  # fmt: skip


def test_cjk_text_moves_to_a_cjk_family(bundle):
    plan = bundle.plan(
        {"foo sans", "ms mincho"}, [drawn("Foo Sans", "OP " + KANJI), drawn("MS Mincho", KANJI)]
    )
    by = {s.missing: s for s in plan.substitutions}
    assert (
        by["foo sans"].family == "Noto Sans CJK JP"
        and "cjk: the text has CJK characters" in by["foo sans"].reasons
    )
    assert by["ms mincho"].family == "Noto Serif CJK JP"


def test_narrow_font_gets_a_width_correction_and_forced_bold(bundle):
    plan = bundle.plan({"impact"}, [drawn("Impact", "SIGN")])
    (s,) = plan.substitutions
    assert (s.family, s.faces, s.substitute.bold, s.substitute.scale_x) == (
        "Noto Sans",
        ("Bold",),
        True,
        NARROW,
    )


def test_nothing_covers_everything_best_coverage_wins(bundle):
    plan = bundle.plan({"foo"}, [drawn("Foo", "Aж☃")])  # nobody has the snowman
    (s,) = plan.substitutions
    assert s.family == "Noto Sans" and s.uncovered == 1
    assert "no bundled family covers every character" in s.reasons


def test_unused_missing_font_and_empty_bundle(bundle, tmp_path):
    assert bundle.plan({"foo"}, [drawn("Bar", "A")]).substitutions == []  # never drawn: nothing to do
    empty = fallback.FallbackLibrary(tmp_path / "nothing")
    plan = empty.plan({"foo"}, [drawn("Foo", "A")])
    assert plan.unresolved == ["foo"] and not plan.faces


def test_host_fonts_outside_the_bundle_are_ignored(bundle):
    assert "Some Host Font" not in {f.family for f in bundle.faces}


def test_subset_has_exactly_the_drawn_characters_and_keeps_the_family(bundle):
    plan = bundle.plan({"foo"}, [drawn("Foo", "Привет")])
    (face,) = plan.faces
    font = TTFont(io.BytesIO(face.data))
    # + space: libass would fetch it from a system font otherwise (the fake font has no NBSP)
    assert set(font.getBestCmap()) == {ord(c) for c in "Привет "}
    assert "noto sans" in face.families
    again = bundle.plan({"foo"}, [drawn("Foo", "Привет")])
    assert again.faces[0].data == face.data and again.faces[0].filename == face.filename  # deterministic
