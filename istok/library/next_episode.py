"""Which file comes after the one being watched, and which pieces to prefetch for it. Pure.

Next episode: same title tokens (`series`) and season, the smallest episode number greater than the
current one (`Title - 05` → `Title - 06`, `S01E09` → `S01E10`, `05` → `05.5` if that exists). An OVA or
another season is a different series and never "next". Files without episode numbers: the next video in
natural order in the same folder.

Prefetch = the first PREFETCH_HEAD and the last PREFETCH_TAIL bytes of that video (headers, first minutes;
MKV Cues live at the end) plus its small sidecars (subtitles) whole and the head/tail of sidecar audio.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from istok.subs.model import FileName, Kind


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
            and f.series == current.series
            and f.episode.season == current.episode.season
            and f.episode.number > current.episode.number
        ]
        if same:
            return min(same, key=lambda f: (f.episode.number, f.path))  # type: ignore[union-attr]
        return None
    siblings = sorted((f for f in videos if f.folders == current.folders), key=lambda f: _natural_key(f.path))
    after = [f for f in siblings if _natural_key(f.path) > _natural_key(current.path)]
    return after[0] if after else None


def head_tail_pieces(offset: int, length: int, piece_length: int, head: int, tail: int) -> list[int]:
    """Pieces covering the first `head` and the last `tail` bytes of a file at `offset` in the torrent."""
    if length <= 0 or piece_length <= 0:
        return []
    ranges = [(offset, offset + min(head, length) - 1)]
    if tail > 0:
        ranges.append((offset + max(0, length - tail), offset + length - 1))
    pieces: set[int] = set()
    for start, end in ranges:
        if end >= start:
            pieces.update(range(start // piece_length, end // piece_length + 1))
    return sorted(pieces)
