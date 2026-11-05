"""Decide the state of every torrent. Pure: snapshots in, decisions (with reasons) out.

Like TorrServer and the original Stremio server (analysis/20-streaming-clients.md): only what is being watched
downloads; whatever is downloaded keeps seeding.

    1. open streams, or any activity (create, stats, stream) less than STREAM_GRACE ago → streaming
         the opened episode downloads at full speed; once it (with its sidecars) is complete, the next episode
         downloads too (`Decision.prefetch`) — one episode ahead, and only while someone watches
       our ffmpeg still reads a file (an audio repack the viewer started)  → streaming, no next episode:
         a repack must not hang on data that no longer downloads
    2. seeding ratio or seeding time limit reached                     → done (paused)
    3. everything else                                                 → seeding (upload only)

The next episode waits while any watched episode (in any torrent) still downloads: the line is the stream's.
"""

from __future__ import annotations

from collections.abc import Sequence

from protoka.library.model import Decision, Limits, State, TorrentView
from protoka.logfmt import size


def is_streaming(v: TorrentView, limits: Limits, now: float) -> bool:
    if v.open_streams > 0:
        return True
    return v.last_activity is not None and now - v.last_activity < limits.stream_grace


def line_free(views: Sequence[TorrentView], limits: Limits, now: float) -> tuple[bool, str]:
    """May the next episode download? Not while an episode being watched still needs the line."""
    busy = [v for v in views if is_streaming(v, limits, now) and v.remaining_bytes > 0]
    if busy:
        return False, f"{busy[0].name!r} is being watched and still downloading"
    return True, "the episode being watched is downloaded"


def _limit_reason(v: TorrentView, limits: Limits, now: float) -> str | None:
    if limits.seed_ratio > 0 and v.downloaded and v.ratio >= limits.seed_ratio:
        return f"ratio {v.ratio:.2f} >= limit {limits.seed_ratio:g}"
    if limits.seed_time > 0 and v.seeding_since is not None and now - v.seeding_since >= limits.seed_time:
        hours = (now - v.seeding_since) / 3600
        return f"seeded {hours:.1f} h >= limit {limits.seed_time / 3600:g} h"
    return None


def decide(views: Sequence[TorrentView], limits: Limits, now: float) -> list[Decision]:
    free, why = line_free(views, limits, now)
    out = []
    for v in views:
        if is_streaming(v, limits, now):
            reasons = [
                f"{v.open_streams} open stream(s)" if v.open_streams else "requested recently (stream grace)"
            ]
            prefetch = v.prefetch_pending and v.remaining_bytes == 0 and free
            if prefetch:
                reasons.append("downloading the next episode")
            elif v.prefetch_pending and v.remaining_bytes == 0:
                reasons.append(f"next episode waits: {why}")
            out.append(Decision(v.ih, State.STREAMING, tuple(reasons), prefetch=prefetch))
        elif v.internal_reads > 0:
            out.append(Decision(v.ih, State.STREAMING, ("our ffmpeg reads a file (audio repack)",)))
        elif (reason := _limit_reason(v, limits, now)) is not None:
            out.append(Decision(v.ih, State.DONE, (reason,)))
        elif v.remaining_bytes > 0 or v.prefetch_pending:
            left = (
                f"{size(v.remaining_bytes)} of the opened files" if v.remaining_bytes else "the next episode"
            )
            out.append(
                Decision(v.ih, State.SEEDING, (f"not watched: seeding what is here, not fetching {left}",))
            )
        else:
            out.append(Decision(v.ih, State.SEEDING, ("all wanted pieces are here",)))
    return out
