"""Decide the state of every torrent. Pure: snapshots in, decisions (with reasons) out.

Rules, first match wins:
    1. open streams, or any activity (create, stats, stream) less than STREAM_GRACE ago → streaming
    2. seeding ratio or seeding time limit reached                     → done
    3. opened files not fully downloaded                               → completing | waiting
    4. the next episode's head/tail not downloaded yet (prefetch)      → prefetching | waiting
    5. everything else                                                 → seeding (libtorrent's queue)

Background downloads (3 and 4) only run when *background is allowed* — no streaming torrent still has
bytes to download — and at most `bg_downloads` torrents at once, the most recently watched first;
completing goes before prefetching. Everything else that has work to do waits in upload-only mode.
A streaming torrent whose watched file is already downloaded may prefetch its next episode too
(state stays `streaming`, `Decision.prefetch` is set) — that is when prefetching is most useful.
"""

from __future__ import annotations

from collections.abc import Sequence

from istok.library.model import Decision, Limits, State, TorrentView


def is_streaming(v: TorrentView, limits: Limits, now: float) -> bool:
    if v.open_streams > 0:
        return True
    return v.last_activity is not None and now - v.last_activity < limits.stream_grace


def background_allowed(views: Sequence[TorrentView], limits: Limits, now: float) -> tuple[bool, str]:
    busy = [v for v in views if is_streaming(v, limits, now) and v.remaining_bytes > 0]
    if busy:
        return False, f"{busy[0].name!r} is being watched and still downloading"
    return True, "nothing being watched needs the line"


def _limit_reason(v: TorrentView, limits: Limits, now: float) -> str | None:
    if limits.seed_ratio > 0 and v.downloaded and v.ratio >= limits.seed_ratio:
        return f"ratio {v.ratio:.2f} >= limit {limits.seed_ratio:g}"
    if limits.seed_time > 0 and v.seeding_since is not None and now - v.seeding_since >= limits.seed_time:
        hours = (now - v.seeding_since) / 3600
        return f"seeded {hours:.1f} h >= limit {limits.seed_time / 3600:g} h"
    return None


def decide(views: Sequence[TorrentView], limits: Limits, now: float) -> list[Decision]:
    allowed, why = background_allowed(views, limits, now)
    decisions: dict[str, Decision] = {}
    completing: list[TorrentView] = []
    prefetching: list[TorrentView] = []

    watched_prefetch: list[TorrentView] = []  # being watched, fully downloaded, next episode not yet
    for v in views:
        if is_streaming(v, limits, now):
            detail = (
                f"{v.open_streams} open stream(s)" if v.open_streams else "requested recently (stream grace)"
            )
            decisions[v.ih] = Decision(v.ih, State.STREAMING, (detail,))
            if v.prefetch_pending and v.remaining_bytes == 0:
                watched_prefetch.append(v)
        elif (reason := _limit_reason(v, limits, now)) is not None:
            decisions[v.ih] = Decision(v.ih, State.DONE, (reason,))
        elif v.remaining_bytes > 0:
            completing.append(v)
        elif v.prefetch_pending:
            prefetching.append(v)  # not watched any more but the next episode was not fetched yet
        else:
            decisions[v.ih] = Decision(v.ih, State.SEEDING, ("all wanted pieces are here",))

    slots = max(0, limits.bg_downloads)
    for group, state, what in (
        (completing, State.COMPLETING, "finishing opened files"),
        (prefetching, State.PREFETCHING, "prefetching the next episode"),
    ):
        for v in sorted(group, key=lambda x: -x.last_watched):  # most recently watched first
            if not allowed:
                decisions[v.ih] = Decision(v.ih, State.WAITING, (f"{what} paused: {why}",))
            elif slots > 0:
                slots -= 1
                decisions[v.ih] = Decision(v.ih, state, (what, why), prefetch=state is State.PREFETCHING)
            else:
                decisions[v.ih] = Decision(
                    v.ih, State.WAITING, (f"{what} queued: BG_DOWNLOADS={limits.bg_downloads} in use",)
                )
    # The usual case: the next episode is in the torrent being watched, and the current one is already
    # downloaded long before it ends. It stays `streaming` but may prefetch, within the same slots.
    for v in sorted(watched_prefetch, key=lambda x: -x.last_watched):
        if allowed and slots > 0:
            slots -= 1
            d = decisions[v.ih]
            decisions[v.ih] = Decision(
                v.ih, d.state, (*d.reasons, "prefetching the next episode"), prefetch=True
            )
    return [decisions[v.ih] for v in views]
