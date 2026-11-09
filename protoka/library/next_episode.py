"""Which file comes after the one being watched, and which files make up an episode. Pure.

Next episode: same show and season, the smallest episode number greater than the current one
(`Title - 05` → `Title - 06`, `S01E09` → `S01E10`, `05` → `05.5` if that exists). The show is the title
before the episode number (`FileName.show`), so an episode title after it does not count
(`Show.S01E01.Part.I…` → `Show.S01E02.Part.II…`); a name with nothing before the number falls back to all
its title tokens. An OVA or another season is a different series and never "next". Files without episode
numbers: the next video in natural order in the same folder.

An episode = the video and every file playing it needs on disk: matched subtitles, matched external audio
and the font files its subtitles may use. The next episode is downloaded whole,
all of these files at file priority, so their data goes to the real files (never to libtorrent's part file:
analysis/18-part-file.md).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from protoka.subs import fonts, matching
from protoka.subs.model import FileName, Kind


def _natural_key(path: str) -> list[object]:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path)]


def next_video(files: Sequence[FileName], current_idx: int) -> FileName | None:
    current = files[current_idx]
    videos = [f for f in files if f.kind is Kind.VIDEO and f.index != current_idx]
    if current.episode is not None:
        same = [
            f
            for f in videos
            if f.episode is not None
            and _show(f) == _show(current)
            and f.episode.season == current.episode.season
            and f.episode.number > current.episode.number
        ]
        if same:
            return min(same, key=lambda f: (f.episode.number, f.path))  # type: ignore[union-attr]
        return None
    siblings = sorted((f for f in videos if f.folders == current.folders), key=lambda f: _natural_key(f.path))
    after = [f for f in siblings if _natural_key(f.path) > _natural_key(current.path)]
    return after[0] if after else None


def _show(f: FileName) -> tuple[str, ...]:
    return f.show or f.series


def episode_files(files: Sequence[FileName], video_idx: int) -> list[int]:
    """The video first, then its subtitles, external audio and font sources (torrent file indices)."""
    out = [video_idx]
    for kind in (Kind.SUBTITLE, Kind.AUDIO):
        out += [m.sub.index for m in matching.assign(files, kind).by_video.get(video_idx, [])]
    out += [src.file_idx for src in fonts.locate(files, files[video_idx])]
    return list(dict.fromkeys(out))
