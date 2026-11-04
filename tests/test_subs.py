"""Unit tests of the pure subtitle pipeline steps (no torrent, no network)."""

from __future__ import annotations

import io
import shutil
import zipfile

import py7zr
import pytest

from istok.subs import archives, ass, convert, fonts, matching, names
from istok.subs.model import Episode, Kind, Match
from istok.subs.service import build_track
from tests.media import ass_script, make_font, srt_script


# -- names ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path,episode",
    [
        ("[Grp] Title - 05 [720p][ABCD1234].mkv", Episode(5)),
        ("RUS Subs/[Grp] Title - 05.ass", Episode(5)),
        ("Title_05v2.mkv", Episode(5)),
        ("Subs/05.ssa", Episode(5)),
        ("Title ep05.avi", Episode(5)),
        ("Title #05.mkv", Episode(5)),
        ("Title - 05.5.mkv", Episode(5.5)),
        ("Title.Ep.12.5.mkv", Episode(12.5)),
        ("Show.S01E02.rus.srt", Episode(2, 1)),
        ("Show/Show.S01E02.1080p.mkv", Episode(2, 1)),
        ("[Grp] Title (2006) - 12 [DVD][1A2B3C4D].mkv", Episode(12)),
        ("Title 26 (2004).mkv", Episode(26)),
        ("Movie.2019.1080p.mkv", None),  # year and resolution are not episodes
        ("[Grp] Movie [1080p][ABCD1234].mkv", None),  # neither is a CRC
        ("Subs/Show.S01E02/2_English.srt", Episode(2, 1)),  # track index; episode from the folder
    ],
)
def test_parse_episode(path, episode):
    assert names.parse_file(0, path).episode == episode


def test_parse_file_fields():
    f = names.parse_file(3, "T/RUS Subs/[Grp] Title OVA - 01 [Ancord].rus.signs.ass", 123)
    assert f.kind is Kind.SUBTITLE and f.ext == ".ass" and f.size == 123
    assert f.group == "Grp"
    assert f.series == ("title", "ova", "ancord")
    assert f.lang_tokens == ("rus", "signs")
    assert f.folders == ("T", "RUS Subs")


@pytest.mark.parametrize(
    "sub,video,lang,label_contains",
    [
        ("T/Title - 01.rus.ass", "T/Title - 01.mkv", "rus", "rus"),
        ("T/RUS Subs/Title - 01.ass", "T/Title - 01.mkv", "rus", "RUS Subs"),
        ("T/Subtitles/English/01.srt", "T/Title - 01.mkv", "eng", "English"),
        ("T/Звук/[AniDUB]/Title - 01.ac3", "T/Title - 01.mkv", "rus", "[AniDUB]"),  # Cyrillic folder → rus
        ("T/Озвучка/Студія/Title - 01.mka", "T/Title - 01.mkv", "ukr", "Студія"),
        ("T/RUS Sound/[Grp] Title - 01.mka", "T/[Grp] Title - 01 [720p].mkv", "rus", "RUS Sound"),
        ("S/Subs/Show.S01E02/3_Russian.srt", "S/Show.S01E02.mkv", "rus", "3_Russian"),
        ("T/Subs/[Ancord]/Title - 01.ass", "T/Title - 01.mkv", None, "[Ancord]"),
        ("T/Надписи/Title - 01.ass", "T/Title - 01.mkv", "rus", "Надписи"),  # Cyrillic folder → rus
        ("Sintel/Sintel.nl.srt", "Sintel/Sintel.mp4", "dut", "nl"),
    ],
)
def test_parse_lang(sub, video, lang, label_contains):
    got_lang, label = names.parse_lang(names.parse_file(1, sub), names.parse_file(0, video))
    assert got_lang == lang
    assert label_contains in label


# -- matching (table of release layouts) -----------------------------------------------------------
LAYOUTS = {
    "single movie, subs anywhere": (
        ["M/Movie.2019.mkv", "M/Movie.2019.rus.srt", "M/Subs/eng.ass", "M/Extras/notes.txt"],
        {"M/Movie.2019.mkv": ["M/Movie.2019.rus.srt", "M/Subs/eng.ass"]},
    ),
    "anime: group tags, CRC, RUS Subs folder, OVA in the same torrent": (
        [
            "T/[Grp] Title - 01 [720p][AAAA1111].mkv",
            "T/[Grp] Title - 02 [720p][BBBB2222].mkv",
            "T/[Grp] Title OVA - 01 [720p][CCCC3333].mkv",
            "T/RUS Subs/[Grp] Title - 01.ass",
            "T/RUS Subs/[Grp] Title - 02.ass",
            "T/RUS Subs/[Grp] Title OVA - 01.ass",
            "T/Fonts/arial.ttf",
        ],
        {
            "T/[Grp] Title - 01 [720p][AAAA1111].mkv": ["T/RUS Subs/[Grp] Title - 01.ass"],
            "T/[Grp] Title - 02 [720p][BBBB2222].mkv": ["T/RUS Subs/[Grp] Title - 02.ass"],
            "T/[Grp] Title OVA - 01 [720p][CCCC3333].mkv": ["T/RUS Subs/[Grp] Title OVA - 01.ass"],
        },
    ),
    "bare episode numbers in Subs/": (
        ["T/Title_01.mkv", "T/Title_02.mkv", "T/Subs/01.ssa", "T/Subs/02.ssa"],
        {"T/Title_01.mkv": ["T/Subs/01.ssa"], "T/Title_02.mkv": ["T/Subs/02.ssa"]},
    ),
    "subs named after the video with extra labels": (
        [
            "T/Title - 01 [BD].mkv",
            "T/Title - 02 [BD].mkv",
            "T/Title - 01 [BD].rus.signs.ass",
            "T/Title - 01 [BD].rus.full.ass",
            "T/Title - 02 [BD].eng.ass",
        ],
        {
            "T/Title - 01 [BD].mkv": ["T/Title - 01 [BD].rus.full.ass", "T/Title - 01 [BD].rus.signs.ass"],
            "T/Title - 02 [BD].mkv": ["T/Title - 02 [BD].eng.ass"],
        },
    ),
    "rarbg-style Subs/<video>/N_Language.srt": (
        [
            "S/Show.S01E01.1080p.mkv",
            "S/Show.S01E02.1080p.mkv",
            "S/Subs/Show.S01E01.1080p/2_English.srt",
            "S/Subs/Show.S01E02.1080p/2_English.srt",
            "S/Subs/Show.S01E02.1080p/3_Russian.srt",
        ],
        {
            "S/Show.S01E01.1080p.mkv": ["S/Subs/Show.S01E01.1080p/2_English.srt"],
            "S/Show.S01E02.1080p.mkv": [
                "S/Subs/Show.S01E02.1080p/2_English.srt",
                "S/Subs/Show.S01E02.1080p/3_Russian.srt",
            ],
        },
    ),
    "season folders": (
        [
            "Show/Season 1/Show 01.mkv",
            "Show/Season 2/Show 01.mkv",
            "Show/Season 1/Show 01.srt",
            "Show/Season 2/Show 01.srt",
        ],
        {
            "Show/Season 1/Show 01.mkv": ["Show/Season 1/Show 01.srt"],
            "Show/Season 2/Show 01.mkv": ["Show/Season 2/Show 01.srt"],
        },
    ),
}


AUDIO_LAYOUTS = {
    "anime: RUS Sound/*.mka and Звук/<group>/*.ac3 next to subtitles": (
        [
            "T/[Grp] Title - 01 [720p].mkv",
            "T/[Grp] Title - 02 [720p].mkv",
            "T/RUS Sound/[Grp] Title - 01.mka",
            "T/RUS Sound/[Grp] Title - 02.mka",
            "T/Звук/[AniDUB]/Title - 01.ac3",
            "T/Звук/[AniDUB]/Title - 02.ac3",
            "T/RUS Subs/[Grp] Title - 01.ass",  # a subtitle: never matched as audio
        ],
        {
            "T/[Grp] Title - 01 [720p].mkv": [
                "T/RUS Sound/[Grp] Title - 01.mka",
                "T/Звук/[AniDUB]/Title - 01.ac3",
            ],
            "T/[Grp] Title - 02 [720p].mkv": [
                "T/RUS Sound/[Grp] Title - 02.mka",
                "T/Звук/[AniDUB]/Title - 02.ac3",
            ],
        },
    ),
    "movie with a dub and a commentary track": (
        ["M/Movie.2010.mkv", "M/Movie.2010.rus.ac3", "M/Audio/Movie.2010.commentary.eng.flac"],
        {"M/Movie.2010.mkv": ["M/Movie.2010.rus.ac3", "M/Audio/Movie.2010.commentary.eng.flac"]},
    ),
}


@pytest.mark.parametrize("layout", AUDIO_LAYOUTS, ids=list(AUDIO_LAYOUTS))
def test_audio_matching_layouts(layout):
    paths, expected = AUDIO_LAYOUTS[layout]
    files = [names.parse_file(i, p) for i, p in enumerate(paths)]
    result = matching.assign(files, Kind.AUDIO)
    got = {paths[v]: sorted(m.sub.path for m in ms) for v, ms in result.by_video.items() if ms}
    every = [m for ms in result.by_video.values() for m in ms] + result.unmatched
    assert got == {k: sorted(v) for k, v in expected.items()}, "\n" + _explain(every)


def _explain(matches: list[Match]) -> str:
    return "\n".join(
        f"  {m.sub.path} -> {m.video.path}: {m.score} {[r.rule for r in m.reasons]}" for m in matches
    )


@pytest.mark.parametrize("layout", LAYOUTS, ids=list(LAYOUTS))
def test_matching_layouts(layout):
    paths, expected = LAYOUTS[layout]
    files = [names.parse_file(i, p) for i, p in enumerate(paths)]
    result = matching.assign(files)
    got = {paths[v]: sorted(m.sub.path for m in ms) for v, ms in result.by_video.items() if ms}
    every = [m for ms in result.by_video.values() for m in ms] + result.unmatched
    assert got == {k: sorted(v) for k, v in expected.items()}, "\n" + _explain(every)


def test_torrent_root_named_like_the_video_is_not_evidence():
    files = [names.parse_file(0, "Sintel/Sintel.mp4"), names.parse_file(1, "Sintel/Sintel.de.srt")]
    match = matching.evaluate(files[0], files[1], matching.Context([files[0]]))
    assert "folder_named_after_video" not in {r.rule for r in match.reasons}


def test_unmatched_subtitles_are_reported_with_reasons():
    paths = ["T/Title - 01.mkv", "T/Title - 02.mkv", "T/Other Show - 07.srt"]
    result = matching.assign([names.parse_file(i, p) for i, p in enumerate(paths)])
    assert not any(result.by_video.values())
    (m,) = result.unmatched
    assert m.sub.path == "T/Other Show - 07.srt"
    assert any(r.rule == "below_threshold" for r in m.reasons)
    assert any(r.rule == "different_episode" for r in m.reasons)


# -- convert ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "data,encoding",
    [
        ("Привет, мир! Это субтитры к первой серии, перевод сделан с японского.".encode("cp1251"), "cp1251"),
        ("Привет".encode(), "utf-8"),
        ("\ufeffПривет".encode(), "utf-8"),
        ("Привет".encode("utf-16"), "utf-16"),
        ("こんにちは、世界。日本語の字幕です。".encode("shift_jis"), "cp932"),  # cp932 ⊃ Shift-JIS
    ],
)
def test_decode_text(data, encoding):
    text, enc = convert.decode_text(data)
    assert text == data.decode(encoding).lstrip("\ufeff")
    assert enc.replace("-", "_").lower().startswith(encoding.replace("-", "_").lower())


def test_srt_parse_and_render():
    text = srt_script(
        [("00:00:01,000", "00:00:02,500", "One\nline two"), ("00:01:00.5", "00:01:01,000", "<b>Two</b>")]
    )
    events = convert.parse_srt(text)
    assert [(e.start_ms, e.end_ms) for e in events] == [(1000, 2500), (60500, 61000)]
    assert events[0].payload == "One\nline two"
    assert "00:00:01,000 --> 00:00:02,500" in convert.to_srt(events)
    assert convert.to_vtt(events).startswith("WEBVTT\n\n00:00:01.000 --> 00:00:02.500")


def test_vtt_parse():
    text = "WEBVTT\n\nNOTE comment\n\n00:01.000 --> 00:02.000 align:start\n<v Bob><i>Hi</i></v>\n\n1\n00:00:03.000 --> 00:00:04.000\nBye\n"
    events = convert.parse_vtt(text)
    assert [(e.start_ms, e.payload) for e in events] == [(1000, "<i>Hi</i>"), (3000, "Bye")]


# -- ass -------------------------------------------------------------------------------------------
def test_parse_ass_to_matroska():
    text = ass_script(
        [("0:00:01.00", "0:00:02.50", "Hello, world"), ("0:00:00.50", "0:00:01.00", "{\\fnOther}First")]
    ).replace("[Events]", "[Fonts]\nfontname: x.ttf\n!!!!\n\n[Events]")
    private, events, is_ssa = ass.parse_ass(text)
    assert not is_ssa
    assert private.startswith("[Script Info]") and private.rstrip().endswith("Effect, Text")
    assert "[Fonts]" not in private
    assert [(e.start_ms, e.end_ms) for e in events] == [(500, 1000), (1000, 2500)]
    # ReadOrder follows file order; text keeps its commas
    assert events[1].payload == "0,0,Default,,0,0,0,,Hello, world"
    assert events[0].payload == "1,0,Default,,0,0,0,,{\\fnOther}First"
    assert ass.plain_text(events[0].payload) == "First"


def test_parse_ssa_v4():
    text = (
        "[Script Info]\nScriptType: v4.00\n\n[V4 Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, TertiaryColour, BackColour, Bold, Italic,"
        " BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, AlphaLevel, Encoding\n"
        "Style: Default,@MS Gothic,20,1,1,1,1,0,0,1,1,0,2,10,10,10,0,128\n\n[Events]\n"
        "Format: Marked, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: Marked=0,0:00:01.00,0:00:02.00,Default,,0000,0000,0000,,Text\n"
    )
    _, events, is_ssa = ass.parse_ass(text)
    assert is_ssa
    assert events[0].payload == "0,0,Default,,0000,0000,0000,,Text"
    assert ass.font_names(text) == {"ms gothic"}


def test_font_names():
    text = ass_script([("0:00:01.00", "0:00:02.00", "{\\fnComic Sans\\b1}x{\\fn@Meiryo}y")], font="Test Sans")
    assert ass.font_names(text) == {"test sans", "comic sans", "meiryo"}


def test_parse_ass_without_events_fails():
    with pytest.raises(ass.AssError):
        ass.parse_ass("[Script Info]\nTitle: x\n")


def test_build_track_srt_and_ass():
    video = names.parse_file(0, "T/Title - 01.mkv")
    line = "Привет! Это субтитры к первой серии."
    srt = build_track(
        Match(video, names.parse_file(1, "T/Title - 01.rus.srt"), []),
        srt_script([("00:00:01,000", "00:00:02,000", line)]).encode("cp1251"),
    )
    assert (srt.codec_id, srt.lang, srt.encoding) == ("S_TEXT/UTF8", "rus", "cp1251")
    assert srt.events[0].payload == line
    a = build_track(
        Match(video, names.parse_file(2, "T/Title - 01.ass"), []),
        ass_script([("0:00:01.00", "0:00:02.00", "x")]).encode(),
    )
    assert a.codec_id == "S_TEXT/ASS" and a.font_names == {"test sans"}


# -- fonts / archives ----------------------------------------------------------------------------
def test_font_families_and_pick():
    face = fonts.make_face("1", "Fonts/TestSans.ttf", make_font("Test Sans"))
    assert "test sans" in face.families
    assert face.mime == "application/x-truetype-font"
    other = fonts.make_face("2", "o.otf", make_font("Other"))
    picked, missing = fonts.pick({"Test Sans", "Absent"}, [face, other])
    assert picked == [face] and missing == {"absent"}


def test_broken_font_has_no_families():
    assert fonts.families(b"not a font") == frozenset()


def test_locate_fonts_nearest_first():
    paths = [
        "T/Title - 01.mkv",
        "T/Fonts/b.ttf",
        "Other/Fonts/far.ttf",
        "T/Fonts.rar",
        "T/Extras/readme.zip",  # not a font archive
        "T/Шрифты/a.otf",
    ]
    files = [names.parse_file(i, p) for i, p in enumerate(paths)]
    located = [s.path for s in fonts.locate(files, files[0])]
    assert located == ["T/Fonts/b.ttf", "T/Шрифты/a.otf", "T/Fonts.rar", "Other/Fonts/far.ttf"]


def test_extract_fonts_zip_and_7z(tmp_path):
    font = make_font("Zip Sans")
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("Fonts/ZipSans.ttf", font)
        z.writestr("readme.txt", "x")
    assert archives.extract_fonts("Fonts.zip", zbuf.getvalue()) == [("Fonts/ZipSans.ttf", font)]

    sbuf = io.BytesIO()
    with py7zr.SevenZipFile(sbuf, "w") as z:
        z.writestr(font, "Fonts/ZipSans.ttf")
        z.writestr(b"x", "readme.txt")
    assert archives.extract_fonts("fonts.7z", sbuf.getvalue()) == [("Fonts/ZipSans.ttf", font)]


def test_extract_fonts_broken_archive():
    with pytest.raises(archives.ArchiveUnsupported):
        archives.extract_fonts("Fonts.zip", b"garbage")


@pytest.mark.skipif(not shutil.which("rar"), reason="needs the `rar` tool to create a test archive")
def test_extract_fonts_rar(tmp_path):
    import subprocess

    (tmp_path / "F.ttf").write_bytes(make_font("Rar Sans"))
    subprocess.run(
        ["rar", "a", "-ep", str(tmp_path / "f.rar"), str(tmp_path / "F.ttf")], check=True, capture_output=True
    )
    assert archives.extract_fonts("Fonts.rar", (tmp_path / "f.rar").read_bytes())[0][0] == "F.ttf"
