"""Step 2: decide which subtitle file belongs to which video. Pure and explainable.

Every rule is a small function `(video, sub, ctx) -> Evidence | None`. A subtitle's score for a video is
the sum of the points of all rules that fired; the subtitle goes to the video with the best score if it
reaches THRESHOLD. The evidence list is kept and shown in `subtitles.json` → `match.reasons`, so a wrong
decision can be read, not guessed. A new kind of release layout = a new rule (+ a row in the table test).

Weights (points):
    only_video_in_torrent         +100  the only video — every subtitle is its subtitle
    same_stem_prefix              +100  "Title - 05.rus.ass" starts with "Title - 05"
    folder_named_after_video      +90   "Subs/Title - 05/2_English.srt"
    same_episode                  +50   episode numbers equal (season too, when both have one)
    different_episode             -100  episode numbers differ: almost certainly another episode
    same_series                   +20   title tokens agree ("title" vs "title ancord")
    different_series              -30   both have a title and they disagree (OVA vs main series)
    similar_name                  0…+30 difflib similarity of cleaned names (only if ≥ 0.5)
    near_video                    +10   the subtitle is in the video's folder or below it
THRESHOLD = 50
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from protoka.subs.model import Assignment, Evidence, FileName, Kind, Match

THRESHOLD = 50


@dataclass(frozen=True)
class Context:
    videos: Sequence[FileName]

    @property
    def video_count(self) -> int:
        return len(self.videos)


Rule = Callable[[FileName, FileName, Context], Evidence | None]


def only_video_in_torrent(video: FileName, sub: FileName, ctx: Context) -> Evidence | None:
    if ctx.video_count == 1:
        return Evidence("only_video_in_torrent", 100, "the torrent has a single video")
    return None


def same_stem_prefix(video: FileName, sub: FileName, ctx: Context) -> Evidence | None:
    if sub.stem.lower().startswith(video.stem.lower()):
        return Evidence("same_stem_prefix", 100, f"{sub.stem!r} starts with {video.stem!r}")
    return None


def folder_named_after_video(video: FileName, sub: FileName, ctx: Context) -> Evidence | None:
    # only folders below the video's own folder: "Sintel/Sintel.mp4" + "Sintel/x.srt" is not evidence
    v = video.stem.lower()
    below = (
        sub.folders[len(video.folders) :]
        if sub.folders[: len(video.folders)] == video.folders
        else sub.folders
    )
    for folder in below:
        if folder.lower() == v:
            return Evidence("folder_named_after_video", 90, f"folder {folder!r}")
    return None


def episode(video: FileName, sub: FileName, ctx: Context) -> Evidence | None:
    ve, se = video.episode, sub.episode
    if ve is None or se is None:
        return None
    same = ve.number == se.number and (ve.season is None or se.season is None or ve.season == se.season)
    where = f" (from {sub.episode_source})" if sub.episode_source.startswith("folder") else ""
    if same:
        return Evidence("same_episode", 50, f"episode {ve} == {se}{where}")
    return Evidence("different_episode", -100, f"episode {ve} != {se}{where}")


def series(video: FileName, sub: FileName, ctx: Context) -> Evidence | None:
    a, b = video.series, sub.series
    if not a or not b:
        return None
    n = min(len(a), len(b))
    if a[:n] == b[:n]:  # one title is a prefix of the other: "title" / "title ancord"
        return Evidence("same_series", 20, f"title {' '.join(a)!r} ~ {' '.join(b)!r}")
    return Evidence("different_series", -30, f"title {' '.join(a)!r} vs {' '.join(b)!r}")


def similar_name(video: FileName, sub: FileName, ctx: Context) -> Evidence | None:
    ratio = SequenceMatcher(None, video.clean, sub.clean).ratio()
    if ratio < 0.5:
        return None
    return Evidence("similar_name", round(ratio * 30), f"name similarity {ratio:.2f}")


def near_video(video: FileName, sub: FileName, ctx: Context) -> Evidence | None:
    if sub.folders[: len(video.folders)] == video.folders:
        return Evidence("near_video", 10, "in the video's folder or below it")
    return None


RULES: tuple[Rule, ...] = (
    only_video_in_torrent,
    same_stem_prefix,
    folder_named_after_video,
    episode,
    series,
    similar_name,
    near_video,
)


def evaluate(video: FileName, sub: FileName, ctx: Context) -> Match:
    reasons = [ev for rule in RULES if (ev := rule(video, sub, ctx)) is not None]
    return Match(video=video, sub=sub, reasons=reasons)


def assign(files: Sequence[FileName], kind: Kind = Kind.SUBTITLE) -> Assignment:
    """Give each sidecar of `kind` (subtitles, external audio) to its best video (ties: to all tied videos).

    The same rules apply to every kind: a sidecar is a file that belongs to one episode."""
    videos = [f for f in files if f.kind is Kind.VIDEO]
    subs = [f for f in files if f.kind is kind]
    ctx = Context(videos)
    result = Assignment(by_video={v.index: [] for v in videos})
    for sub in subs:
        candidates = [evaluate(v, sub, ctx) for v in videos]
        if not candidates:
            result.unmatched.append(
                Match(video=sub, sub=sub, reasons=[Evidence("no_videos", 0, "no video files")])
            )
            continue
        best = max(c.score for c in candidates)
        if best < THRESHOLD:
            top = max(candidates, key=lambda c: c.score)
            top.reasons.append(Evidence("below_threshold", 0, f"best score {best} < {THRESHOLD}"))
            result.unmatched.append(top)
            continue
        winners = [c for c in candidates if c.score == best]
        for w in winners:
            if len(winners) > 1:
                w.reasons.append(Evidence("tie", 0, f"tied with {len(winners) - 1} other video(s)"))
            result.by_video[w.video.index].append(w)
    for matches in result.by_video.values():
        matches.sort(key=lambda m: (-m.score, m.sub.path))
    return result
