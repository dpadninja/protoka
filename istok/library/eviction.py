"""What to delete when the cache is full. Pure.

    used    = Σ size on disk of every cached torrent
    reserve = bytes the cache is about to receive (remaining bytes of streaming / background work)
    evict while  used + reserve > CACHE_SIZE   or   free disk < MIN_FREE_DISK

Victims, in order: never a streaming torrent; first those that are `done` (limits reached), then the
least recently watched (LRU by watching, not by downloading); on a tie the one where we are needed least
(most seeds in the swarm) goes first. A torrent is evicted as a whole.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from istok.library.model import State, TorrentView


@dataclass(frozen=True)
class Plan:
    victims: list[tuple[str, str]]  # (info hash, reason)
    used: int
    reserve: int
    still_short: int  # bytes over budget left after evicting everything allowed (0 = fits)


def choose_victims(
    views: Sequence[TorrentView],
    states: Mapping[str, State],
    *,
    budget: float,
    reserve: int,
    free_disk: int,
    min_free: int,
) -> Plan:
    used = sum(v.size_on_disk for v in views)
    over_budget = used + reserve - budget
    over_disk = min_free - free_disk
    need = max(over_budget, over_disk, 0)
    candidates = [v for v in views if states.get(v.ih) is not State.STREAMING]
    candidates.sort(key=lambda v: (states.get(v.ih) is not State.DONE, v.last_watched, -v.seeds))
    victims: list[tuple[str, str]] = []
    for v in candidates:
        if need <= 0:
            break
        why = "over CACHE_SIZE" if over_budget > 0 else "low free disk space"
        kind = "done seeding" if states.get(v.ih) is State.DONE else "least recently watched"
        victims.append((v.ih, f"{why}: {kind}, frees {v.size_on_disk} bytes"))
        need -= v.size_on_disk
    return Plan(victims=victims, used=int(used), reserve=reserve, still_short=int(max(need, 0)))
