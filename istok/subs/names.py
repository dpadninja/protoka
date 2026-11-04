"""Step 1: turn torrent paths into `FileName` facts (kind, episode number, series tokens, language).

Pure functions, path strings only. Typical old anime releases this has to understand:

    [Grp] Title - 05 [720p][ABCD1234].mkv      episode 5, series ("title",), group "Grp"
    RUS Subs/[Grp] Title - 05.ass              episode 5, lang from folder -> rus
    Title_05v2.mkv / Subs/05.ssa               episode 5
    Title ep05.avi, Title #05, Title - 05.5    episode 5 / 5.5
    Show.S01E02.rus.srt                        S01E02, lang rus

Noise that must never be read as an episode number: CRC32 tags, resolutions, codecs, years.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Any

from istok.guess import parse_season_episode
from istok.subs.model import Episode, FileName, Kind

VIDEO_EXTS = frozenset(
    ".mkv .mk3d .avi .mp4 .m4v .mov .wmv .mpg .mpeg .ts .m2ts .webm .flv .ogm .divx .rmvb".split()
)
SUBTITLE_EXTS = frozenset(".srt .ass .ssa .vtt".split())
MKA_EXTS = frozenset({".mka"})
RAW_AUDIO_EXTS = frozenset(
    ".ac3 .eac3 .ec3 .dts .aac .m4a .flac .mp3 .mp2 .opus .ogg .wav .thd .truehd".split()
)
AUDIO_EXTS = MKA_EXTS | RAW_AUDIO_EXTS
FONT_EXTS = frozenset(".ttf .otf .ttc .otc".split())
ARCHIVE_EXTS = frozenset(".zip .7z .rar".split())

# Words that are never part of a title: release noise.
_NOISE_WORDS = frozenset(
    """
    x264 x265 h264 h265 hevc avc xvid divx 10bit 8bit hi10 hi10p hi444 aac flac ac3 dts mp3 opus
    bd bdrip bluray blu-ray dvd dvdrip web webrip web-dl tv tvrip hdtv hdrip remux raw raws
    dual audio multi subs sub softsub hardsub uncensored uncen
    """.split()
)
_RESOLUTION_RE = re.compile(r"^(?:\d{3,4}p|\d{3,4}x\d{3,4}|4k|2k)$", re.I)
_CRC_RE = re.compile(r"^[0-9a-f]{8}$", re.I)
_YEAR_RE = re.compile(r"^(?:19|20)\d\d$")

# Language vocabulary: token -> ISO 639-2 (Matroska uses 639-2 codes).
LANG_TOKENS: dict[str, str] = {}
for _code, _words in {
    "rus": "ru rus russian рус русский русские russub русск",
    "eng": "en eng english англ",
    "jpn": "ja jp jpn japanese яп",
    "ukr": "uk ukr ukrainian укр",
    "ger": "de ger deu german",
    "fre": "fr fre fra french",
    "spa": "es spa spanish",
    "ita": "it ita italian",
    "por": "pt por portuguese ptbr",
    "chi": "zh chi zho chs cht chinese sc tc gb big5",
    "kor": "ko kor korean",
    "pol": "pl pol polish",
    "dut": "nl dut nld dutch",
    "swe": "sv swe swedish",
    "fin": "fi fin finnish",
    "dan": "da dan danish",
    "nor": "no nor nb nob norwegian",
    "cze": "cs cze ces czech",
    "slo": "sk slo slk slovak",
    "hun": "hu hun hungarian",
    "rum": "ro rum ron romanian",
    "bul": "bg bul bulgarian",
    "srp": "sr srp serbian",
    "hrv": "hr hrv croatian",
    "slv": "sl slv slovenian",
    "gre": "el gre ell greek",
    "tur": "tr tur turkish",
    "ara": "ar ara arabic",
    "heb": "he heb hebrew",
    "per": "fa per fas persian",
    "hin": "hi hin hindi",
    "tha": "th tha thai",
    "vie": "vi vie vietnamese",
    "ind": "id ind indonesian",
    "may": "ms may msa malay",
    "bel": "be bel belarusian бел",
    "kaz": "kk kaz kazakh",
    "lit": "lt lit lithuanian",
    "lav": "lv lav latvian",
    "est": "et est estonian",
}.items():
    for _w in _words.split():
        LANG_TOKENS[_w] = _code

# Folder words that only say "this is a sidecar track" (folders "Subs", "RUS Sound", "Озвучка").
GENERIC_SUBTITLE_WORDS = frozenset(
    "subs sub subtitles subtitle субтитры сабы "
    "sound sounds audio audios dub dubs voice voices track tracks "
    "звук звуки озвучка озвучки озвучание дорожки".split()
)
# Words that distinguish subtitle variants and belong in the label ("Signs", "Надписи", "Full").
LABEL_WORDS = frozenset("signs надписи full forced sdh cc songs karaoke commentary комментарии".split())
SUBTITLE_WORDS = GENERIC_SUBTITLE_WORDS | LABEL_WORDS
FONT_FOLDER_WORDS = frozenset("fonts font шрифты шрифт".split())

_FRACTION_RE = re.compile(r"(?<!\d)(\d{1,4})\.(\d)(?![\d.]|\d*p)")
# "2_English.srt" inside "Subs/<video>/": the number is a track index, not an episode
_TRACK_INDEX_NAME_RE = re.compile(r"^\d{1,2}[ _.-]+\D.*$")
_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")
_UKRAINIAN_RE = re.compile(r"[іїєґІЇЄҐ]")
_BRACKETS_RE = re.compile(r"[\[\(\{]([^\]\)\}]*)[\]\)\}]")
_TOKEN_SPLIT_RE = re.compile(r"[\s._]+")


def classify(ext: str) -> Kind:
    ext = ext.lower()
    if ext in VIDEO_EXTS:
        return Kind.VIDEO
    if ext in SUBTITLE_EXTS:
        return Kind.SUBTITLE
    if ext in AUDIO_EXTS:
        return Kind.AUDIO
    if ext in FONT_EXTS:
        return Kind.FONT
    if ext in ARCHIVE_EXTS:
        return Kind.ARCHIVE
    return Kind.OTHER


def is_noise(token: str) -> bool:
    t = token.lower()
    return (
        t in _NOISE_WORDS
        or bool(_RESOLUTION_RE.match(t))
        or bool(_CRC_RE.match(t) and not t.isdigit())
        or bool(_YEAR_RE.match(t))
    )


def strip_noise(stem: str) -> tuple[str, str | None]:
    """`"[Grp] Title - 05 [720p][ABCD1234]"` -> (`"title - 05"`, `"Grp"`).

    - a leading [..]/(..) tag is the release group;
    - bracket groups made only of noise (CRC, resolution, codec, year) are dropped;
    - a bracket group that is just a number (`[05]`) is kept as that number;
    - other bracket groups keep their content (could be a title part or a label);
    - `_` and `.` become spaces, except a dot inside a number (`05.5`).
    """
    group = None
    s = stem.strip()
    m = re.match(r"^\s*[\[\(]([^\]\)]+)[\]\)]\s*", s)
    if m and not m.group(1).strip().isdigit():
        group = m.group(1).strip()
        s = s[m.end() :]

    def bracket(mb: re.Match[str]) -> str:
        inner = mb.group(1).strip()
        if inner.isdigit():
            return f" {inner} "
        tokens = [t for t in _TOKEN_SPLIT_RE.split(inner) if t]
        if tokens and all(is_noise(t) for t in tokens):
            return " "
        return f" {inner} "

    s = _BRACKETS_RE.sub(bracket, s)
    # dots and underscores are separators — except the dot of a fractional episode ("05.5")
    s = _FRACTION_RE.sub(lambda m: f"{m.group(1)}\0{m.group(2)}", s)
    s = s.replace(".", " ").replace("_", " ").replace("\0", ".")
    tokens = [t for t in s.split() if not is_noise(t)]
    return " ".join(tokens).lower(), group


# Episode patterns, tried in order on the cleaned (lowercase, noise-free) name.
_EPISODE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("dash", re.compile(r"(?:^|\s)-\s*(\d{1,4}(?:\.\d)?)(?:v\d)?(?=\s|$)")),
    ("ep-word", re.compile(r"(?:^|\s)(?:ep|e|episode|серия|эп)\.?\s*(\d{1,4}(?:\.\d)?)(?:v\d)?(?=\s|$)")),
    ("hash", re.compile(r"#\s*(\d{1,4})(?=\s|$)")),
    ("trailing-number", re.compile(r"(?:^|\s)(\d{1,4}(?:\.\d)?)(?:v\d)?$")),
]


def parse_episode(clean: str) -> tuple[Episode | None, tuple[int, int] | None]:
    """Episode number and the (start, end) span it occupies in `clean` (to remove it from the title)."""
    season, eps = parse_season_episode(clean)
    if season is not None and eps:
        m = re.search(r"(?i)s\d{1,3}[ ._-]?e\d{1,4}(?:[ ._-]?e\d{1,4})*|\d{1,2}x\d{1,3}", clean)
        return Episode(float(eps[0]), season), (m.span() if m else None)
    for _name, pat in _EPISODE_PATTERNS:
        for m in pat.finditer(clean):
            num = m.group(1)
            if _YEAR_RE.match(num):
                continue
            return Episode(float(num)), m.span()
    return None, None


def parse_file(index: int, path: str, size: int = 0) -> FileName:
    p = PurePosixPath(path.replace("\\", "/"))
    ext = p.suffix.lower()
    stem = p.name[: -len(p.suffix)] if p.suffix else p.name
    kind = classify(ext)
    clean, group = strip_noise(stem)

    # language tokens: trailing tokens of a subtitle name, e.g. "title 05 rus signs"
    tokens = clean.split()
    lang_tokens: list[str] = []
    sidecar = kind in (Kind.SUBTITLE, Kind.AUDIO)
    if sidecar:
        while tokens and (tokens[-1] in LANG_TOKENS or tokens[-1] in SUBTITLE_WORDS):
            lang_tokens.insert(0, tokens.pop())
    core = " ".join(tokens)

    episode, span = parse_episode(core)
    episode_source = "name" if episode else ""
    if sidecar and _TRACK_INDEX_NAME_RE.match(stem) and not core.replace(" ", "").isalpha():
        episode, span = None, None  # "2_English": track index
    if episode is None and sidecar:
        # "Subs/Show.S01E02/2_English.srt", "Title - 05/rus.ass": the folder names the episode
        for folder in reversed(p.parts[:-1]):
            folder_clean, _ = strip_noise(folder)
            folder_ep, _ = parse_episode(folder_clean)
            if folder_ep:
                episode, episode_source = folder_ep, f"folder {folder!r}"
                break
    title = core if span is None else (core[: span[0]] + " " + core[span[1] :])
    series = tuple(t for t in re.split(r"[\s\-–—~,!:;]+", title) if t and not t.isdigit())
    return FileName(
        index=index,
        path=str(p),
        folders=tuple(p.parts[:-1]),
        stem=stem,
        ext=ext,
        kind=kind,
        size=size,
        clean=clean,
        episode=episode,
        series=series,
        lang_tokens=tuple(lang_tokens),
        group=group,
        episode_source=episode_source,
    )


def parse_files(files: Sequence[dict[str, Any]]) -> list[FileName]:
    """parse-torrent style file dicts (`path`, `length`) -> FileName list (same order/indices)."""
    return [
        parse_file(i, f.get("path") or f.get("name") or "", f.get("length") or 0) for i, f in enumerate(files)
    ]


def folder_tokens(folder: str) -> list[str]:
    clean, _ = strip_noise(folder)
    return clean.split()


def parse_lang(sub: FileName, video: FileName | None = None) -> tuple[str | None, str]:
    """Language (ISO 639-2) and a human label for a subtitle.

    Looks at the subtitle's own trailing tokens first, then at folders below the video's folder
    (`RUS Subs/`, `Subtitles/English/`). The label keeps what distinguishes several translations of
    the same language: non-generic folder names and the part of the name after the video name.
    """
    base_depth = len(video.folders) if video and sub.folders[: len(video.folders)] == video.folders else 0
    extra_folders = sub.folders[base_depth:]

    lang = next((LANG_TOKENS[t] for t in sub.lang_tokens if t in LANG_TOKENS), None)
    if lang is None:
        for folder in reversed(extra_folders):
            lang = next((LANG_TOKENS[t] for t in folder_tokens(folder) if t in LANG_TOKENS), None)
            if lang:
                break
    if lang is None:  # "2_English.srt"-style names
        lang = next((LANG_TOKENS[t] for t in sub.clean.split() if t in LANG_TOKENS), None)
    if lang is None:
        # last resort for Russian-scene releases ("Звук/[AniDUB]", "Озвучка"): Cyrillic names → rus/ukr
        text = " ".join((*extra_folders, sub.stem))
        if _UKRAINIAN_RE.search(text):
            lang = "ukr"
        elif _CYRILLIC_RE.search(text):
            lang = "rus"

    parts: list[str] = []
    for folder in extra_folders:  # "RUS Subs" says nothing new; "[Ancord]" or "Надписи" does
        words = folder_tokens(folder)
        if not words or not all(w in GENERIC_SUBTITLE_WORDS or w in LANG_TOKENS for w in words):
            parts.append(folder)
    rest = sub.stem
    if video and sub.stem.lower().startswith(video.stem.lower()):
        rest = sub.stem[len(video.stem) :]
    elif video and set(sub.clean.split()) <= set(video.clean.split()):
        rest = ""  # "[Grp] Title - 01" next to "[Grp] Title - 01 [720p]": the episode name says nothing new
    rest = rest.strip(" ._-")
    if rest and rest.lower() != (video.stem.lower() if video else ""):
        parts.append(rest)
    if sub.group and sub.group not in " ".join(parts) and (video is None or sub.group != video.group):
        parts.insert(0, f"[{sub.group}]")
    # nothing distinguishing: the subtitle's own folder ("RUS Subs") is still better than a file name
    fallback = extra_folders[-1] if extra_folders else sub.path.rsplit("/", 1)[-1]
    label = " · ".join(dict.fromkeys(p for p in parts if p)) or fallback
    return lang, label
