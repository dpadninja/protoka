"""What to delete when the cache is full. Pure.

    used    = Σ size on disk of every cached torrent
    reserve = bytes the cache is about to receive (remaining bytes of streaming / background work)

Two limits of different strength:
  * free disk < MIN_FREE_DISK — hard: evict anything but a streaming torrent until it is fixed;
  * used + reserve > CACHE_SIZE — soft: only torrents not watched for CACHE_KEEP_RECENT are evicted, and only
    when that actually brings the cache under the limit. The reserve is a forecast (a film closed after ten
    minutes never needs it), so when even evicting everything allowed would not make room for it, only the
    bytes really over the limit count — and if those cannot be freed either, nothing is deleted for nothing
    (analysis/19-eviction.md).

Victims, in order: never a streaming torrent; first those that are `done` (limits reached), then the
least recently watched (LRU by watching, not by downloading); on a tie the one where we are needed least
(most seeds in the swarm) goes first. A torrent is evicted as a whole.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from protoka.library.model import State, TorrentView


@dataclass(frozen=True)
class Plan:
    victims: list[tuple[str, str]]  # (info hash, reason)
    used: int
    reserve: int
    still_short: int  # bytes over budget left after evicting everything allowed, reserve included (0 = fits)
    over_used: int = 0  # bytes really on disk over CACHE_SIZE after the victims are gone (kept: recent)


def choose_victims(
    views: Sequence[TorrentView],
    states: Mapping[str, State],
    *,
    budget: float,
    reserve: int,
    free_disk: int,
    min_free: int,
    now: float = 0.0,
    keep_recent: float = 0.0,
) -> Plan:
    used = sum(v.size_on_disk for v in views)
    candidates = [v for v in views if states.get(v.ih) is not State.STREAMING]
    candidates.sort(key=lambda v: (states.get(v.ih) is not State.DONE, v.last_watched, -v.seeds))

    def recent(v: TorrentView) -> bool:
        return states.get(v.ih) is not State.DONE and keep_recent > 0 and now - v.last_watched < keep_recent

    allowed = sum(v.size_on_disk for v in candidates if not recent(v))  # what the soft limit may delete
    over_budget = used + reserve - budget
    if over_budget > allowed:  # cannot make room for the reserve: only the real overage, if fixable
        over_used = used - budget
        need_budget = over_used if 0 < over_used <= allowed else 0.0
    else:
        need_budget = over_budget
    need_disk = float(min_free - free_disk)
    victims: list[tuple[str, str]] = []
    for v in candidates:
        if need_disk <= 0 and need_budget <= 0:
            break
        if need_disk > 0:
            why = "low free disk space"
        elif not recent(v):
            why = "over CACHE_SIZE"
        else:
            continue
        kind = "done seeding" if states.get(v.ih) is State.DONE else "least recently watched"
        victims.append((v.ih, f"{why}: {kind}, frees {v.size_on_disk} bytes"))
        need_disk -= v.size_on_disk
        need_budget -= v.size_on_disk
    freed = sum(v.size_on_disk for v in views if v.ih in {ih for ih, _ in victims})
    still_short = max(over_budget - freed, need_disk, 0)
    over_used = max(used - freed - budget, 0)
    return Plan(victims, int(used), reserve, int(still_short), int(over_used))
