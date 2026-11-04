"""Data passed between the library steps. No logic here."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class State(StrEnum):
    STREAMING = "streaming"  # someone watches it, or asked for it within STREAM_GRACE
    COMPLETING = "completing"  # finishing the files that were opened, in the background
    PREFETCHING = "prefetching"  # fetching the start/end of the next episode, in the background
    WAITING = "waiting"  # has background work but may not download now: upload only
    SEEDING = "seeding"  # nothing left to fetch; libtorrent's queue decides when it is active
    DONE = "done"  # reached the ratio/time limit: paused, first to be evicted


@dataclass(frozen=True)
class Limits:
    bg_downloads: int = 1  # torrents doing background downloads at once
    seed_ratio: float = 0.0  # 0 = no limit
    seed_time: float = 0.0  # seconds of seeding, 0 = no limit
    stream_grace: float = 120.0  # seconds a torrent stays "streaming" after its last activity


@dataclass(frozen=True)
class TorrentView:
    """What the policy and eviction need to know about one torrent (a snapshot)."""

    ih: str
    name: str
    open_streams: int = 0
    last_activity: float | None = None  # unix time: created, stats polled, stream opened/closed
    remaining_bytes: int = 0  # wanted (opened files) bytes not downloaded yet
    prefetch_pending: bool = False  # the next episode's head/tail is not downloaded yet
    size_on_disk: int = 0
    uploaded: int = 0  # all time
    downloaded: int = 0  # all time
    seeding_since: float | None = None  # unix time when it became complete for what was wanted
    last_watched: float = 0.0  # unix time
    seeds: int = 0  # swarm, from trackers/scrape (num_complete), 0 if unknown
    leechers: int = 0
    has_metadata: bool = True

    @property
    def ratio(self) -> float:
        return self.uploaded / self.downloaded if self.downloaded else 0.0


@dataclass(frozen=True)
class Decision:
    ih: str
    state: State
    reasons: tuple[str, ...] = field(default_factory=tuple)
    prefetch: bool = False  # fetch the next episode's head/tail now (PREFETCHING, or STREAMING + done)

    def to_json(self) -> dict:
        return {"state": self.state.value, "reasons": list(self.reasons), "prefetch": self.prefetch}
