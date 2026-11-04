"""`stats.json` payloads — field-compatible with upstream getStatistics (server.js line 18429)."""

from __future__ import annotations

import os
from typing import Any

import libtorrent as lt
import psutil

from protoka.engine import Engine

_SOURCE_FLAGS = (
    ("tracker", lt.peer_info.tracker),
    ("dht", lt.peer_info.dht),
    ("pex", lt.peer_info.pex),
    ("lsd", lt.peer_info.lsd),
    ("resume", lt.peer_info.resume_data),
)


def _address(p: Any) -> str:
    host, port = p.ip
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def engine_stats(e: Engine | None, file_idx: int | None = None) -> dict[str, Any] | None:
    if e is None:
        return None
    h = e.handle
    st = h.status()
    peers = h.get_peer_info()
    connected = [
        p for p in peers if not (p.flags & lt.peer_info.connecting) and not (p.flags & lt.peer_info.handshake)
    ]
    unchoked = [p for p in connected if not (p.flags & lt.peer_info.choked)]
    by_source = {name: 0 for name, _ in _SOURCE_FLAGS}
    by_source["incoming"] = 0
    for p in connected:
        for name, flag in _SOURCE_FLAGS:
            if p.source & flag:
                by_source[name] += 1
        if not (p.flags & lt.peer_info.local_connection):
            by_source["incoming"] += 1

    s: dict[str, Any] = {"infoHash": e.info_hash}
    if e.metadata_ready.is_set() and e.files is not None:
        s["name"] = e.name
    s.update(
        {
            "peers": len(connected),
            "unchoked": len(unchoked),
            "queued": max(0, st.list_peers - len(peers)),
            "unique": st.list_peers,
            "connectionTries": st.connect_candidates,
            "swarmPaused": bool(st.flags & lt.torrent_flags.paused),
            "swarmConnections": st.num_connections if hasattr(st, "num_connections") else len(peers),
            "swarmSize": st.connections_limit,
            "selections": [sel.to_json() for sel in e.selections],
            "wires": None
            if file_idx is not None
            else [
                {
                    "requests": p.download_queue_length,
                    "address": _address(p),
                    "amInterested": bool(p.flags & lt.peer_info.interesting),
                    "isSeeder": bool(p.flags & lt.peer_info.seed),
                    "downSpeed": p.payload_down_speed,
                    "upSpeed": p.payload_up_speed,
                }
                for p in unchoked
            ],
        }
    )
    if e.files is not None:
        s["files"] = e.files
    s.update(
        {
            "downloaded": st.total_payload_download,
            "uploaded": st.total_payload_upload,
            "downloadSpeed": st.download_payload_rate,
            "uploadSpeed": st.upload_payload_rate,  # upstream returned downloadSpeed here (B1)
            "sources": [src.to_json() for src in e.sources.values()],
            "peerSearchRunning": not bool(st.flags & lt.torrent_flags.paused),
            "opts": e.options,
            # extensions (not in upstream): where connected peers came from, last engine error
            "peerSources": by_source,
            "error": e.error,
        }
    )
    if file_idx is not None and e.files is not None and 0 <= file_idx < len(e.files):
        f = e.files[file_idx]
        start, end = e.file_piece_range(file_idx)
        have = sum(1 for i in range(start, end + 1) if h.have_piece(i))
        total = end - start + 1
        s["streamLen"] = f["length"]
        s["streamName"] = f["name"]
        s["streamProgress"] = have / total if total else 0
    return s


def sys_stats() -> dict[str, Any]:
    freq = psutil.cpu_freq(percpu=True) or []
    cpus = []
    for i, t in enumerate(psutil.cpu_times(percpu=True)):
        speed = int(freq[i].current) if i < len(freq) else 0
        cpus.append(
            {
                "model": "",
                "speed": speed,
                "times": {
                    "user": int(t.user * 1000),
                    "nice": int(getattr(t, "nice", 0) * 1000),
                    "sys": int(t.system * 1000),
                    "idle": int(t.idle * 1000),
                    "irq": int(getattr(t, "irq", 0) * 1000),
                },
            }
        )
    return {"loadavg": list(os.getloadavg()), "cpus": cpus}
