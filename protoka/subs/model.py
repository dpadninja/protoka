"""Data passed between the steps of the subtitle pipeline. No logic here."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Kind(StrEnum):
    VIDEO = "video"
    SUBTITLE = "subtitle"
    AUDIO = "audio"  # external audio track: .mka or a raw stream (.ac3, .dts, .flac, …)
    FONT = "font"
    OTHER = "other"


@dataclass(frozen=True)
class Episode:
    """Episode number parsed from a file name. `number` may be fractional (`05.5` recap episodes)."""

    number: float
    season: int | None = None

    def __str__(self) -> str:
        n = f"{self.number:g}"
        return f"S{self.season:02d}E{n}" if self.season is not None else n


@dataclass(frozen=True)
class FileName:
    """Everything the matcher needs to know about one torrent file, derived from its path only."""

    index: int  # file index in the torrent
    path: str  # "Show/RUS Subs/[Grp] Title - 05.rus.ass"
    folders: tuple[str, ...]  # ("Show", "RUS Subs")
    stem: str  # "[Grp] Title - 05.rus"   (file name without extension)
    ext: str  # ".ass"
    kind: Kind
    size: int
    clean: str  # "title 05 rus"  noise removed, lowercase, separators normalised
    episode: Episode | None  # 5
    series: tuple[str, ...]  # ("title",)  clean tokens without episode number / language tokens
    lang_tokens: tuple[str, ...]  # ("rus",)  language-ish tokens found in the name
    group: str | None  # "Grp"  leading [Group] tag
    episode_source: str = ""  # "name" | "folder 'Show.S01E02'" | "" — for explanations


@dataclass(frozen=True)
class Evidence:
    """One reason for (or against) a subtitle belonging to a video."""

    rule: str
    points: int
    detail: str

    def to_json(self) -> dict:
        return {"rule": self.rule, "points": self.points, "detail": self.detail}


@dataclass
class Match:
    video: FileName
    sub: FileName
    reasons: list[Evidence]

    @property
    def score(self) -> int:
        return sum(e.points for e in self.reasons)


@dataclass
class Assignment:
    """Result of matching: subtitles per video, plus the ones nobody claimed (with their best guess)."""

    by_video: dict[int, list[Match]] = field(default_factory=dict)
    unmatched: list[Match] = field(default_factory=list)


@dataclass(frozen=True)
class SubtitleEvent:
    start_ms: int
    end_ms: int
    # Matroska block payload: for ASS "ReadOrder,Layer,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    # for SRT just the text.
    payload: str


@dataclass
class SubtitleTrack:
    file_idx: int
    source_format: str  # "ass" | "ssa" | "srt" | "vtt"
    codec_id: str  # Matroska CodecID: S_TEXT/ASS | S_TEXT/SSA | S_TEXT/UTF8
    codec_private: str | None  # ASS/SSA script header (everything up to the [Events] Format line)
    events: list[SubtitleEvent]
    text: str  # decoded source text (UTF-8), served by the HTTP API
    encoding: str
    lang: str | None  # ISO 639-2, e.g. "rus"
    label: str
    font_names: frozenset[str] = frozenset()  # fonts referenced by an ASS script (lowercase)


@dataclass(frozen=True)
class FontSource:
    """A font file in the torrent."""

    id: str  # the file index as a string
    file_idx: int
    path: str
    size: int


@dataclass(frozen=True)
class FontFace:
    id: str  # "12" or "13/Fonts/Arial.ttf"
    filename: str  # "Arial.ttf"
    data: bytes
    families: frozenset[str]  # lowercase family / full / PostScript names
    mime: str
