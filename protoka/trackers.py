"""Peer sources: default trackers and merging of client-provided sources.

Upstream *replaces* default sources when `?tr=` or a .torrent announce list is given (P9);
here they are always merged.
"""

from __future__ import annotations

from collections.abc import Iterable

# analysis/server-v4.22.5.js line 72333
DEFAULT_TRACKERS: tuple[str, ...] = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.skynetcloud.site:6969/announce",
    "udp://tracker.qu.ax:6969/announce",
    "udp://tracker.corpscorp.online:80/announce",
    "udp://tracker-udp.gbitt.info:80/announce",
    "udp://tracker.bittor.pw:1337/announce",
    "udp://retracker01-msk-virt.corbina.net:80/announce",
    "udp://tracker.tryhackx.org:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker.ducks.party:1984/announce",
    "udp://tracker2.dler.org:80/announce",
    "udp://tracker.dler.org:6969/announce",
    "http://tracker.dler.com:6969/announce",
    "udp://tracker.nyaa.vc:6969/announce",
    "udp://tracker.peerfect.org:6969/announce",
)

DHT_BOOTSTRAP_NODES = (
    "dht.libtorrent.org:25401",
    "router.bittorrent.com:6881",
    "router.utorrent.com:6881",
    "dht.transmissionbt.com:6881",
    "dht.aelitis.com:6881",
)

_TRACKER_SCHEMES = ("udp://", "http://", "https://", "ws://", "wss://")


def parse_source(src: object) -> tuple[str, str] | None:
    """`"tracker:<url>"` / raw tracker URL → ("tracker", url); `"dht:<ih>"` → ("dht", ih)."""
    if not isinstance(src, str):
        return None
    s = src.strip()
    if s.startswith("tracker:"):
        url = s[len("tracker:") :]
        return ("tracker", url) if url.lower().startswith(_TRACKER_SCHEMES) else None
    if s.startswith("dht:"):
        return ("dht", s[4:].lower())
    if s.lower().startswith(_TRACKER_SCHEMES):
        return ("tracker", s)
    return None


def merge_trackers(*groups: Iterable[object] | None, include_defaults: bool = True) -> list[str]:
    """Ordered, de-duplicated tracker URLs from sources/announce lists (+ defaults)."""
    out: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        key = url.rstrip("/")
        if key not in seen:
            seen.add(key)
            out.append(url)

    for group in groups:
        for src in group or ():
            parsed = parse_source(src)
            if parsed and parsed[0] == "tracker":
                add(parsed[1])
    if include_defaults:
        for url in DEFAULT_TRACKERS:
            add(url)
    return out


def peer_search_sources(trackers: Iterable[str], info_hash: str) -> list[str]:
    """Upstream-style `peerSearch.sources` list for stats/opts."""
    return [f"tracker:{t}" for t in trackers] + [f"dht:{info_hash}"]
