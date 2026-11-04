"""Read the top-level structure of a source MKV: what `layout.build` needs, nothing more.

Works over an async `read(offset, n) -> bytes`, so it only fetches a few small regions of the torrent:
the header up to the first Cluster, the SeekHead targets (usually Cues/Tags at the end), Info, Tracks
and Cues. Cluster contents are never parsed — they are copied byte for byte.

    EBML | Segment [ SeekHead Info Tracks (Attachments) (Chapters) | Cluster … Cluster | Cues (Tags) ]
                    ^ pre-cluster elements                        ^ cluster region    ^ post-cluster
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from protoka.mux.ebml import (
    ID,
    UNKNOWN,
    EbmlError,
    Header,
    iter_children,
    read_float,
    read_header,
    read_uint,
)

Reader = Callable[[int, int], Awaitable[bytes]]

MAX_HEADER_ELEMENTS = 256
MAX_CUES_BYTES = 64 * 1024 * 1024
MAX_SMALL_ELEMENT = 16 * 1024 * 1024  # Info / Tracks / SeekHead


class MkvError(ValueError):
    """The file cannot be handled by the virtual-MKV path (the caller falls back to ffmpeg)."""


@dataclass(frozen=True)
class CuePoint:
    time: int  # in TimestampScale units
    positions: tuple[tuple[int, int], ...]  # (track number, cluster position relative to segment data)


@dataclass
class MkvIndex:
    file_size: int
    ebml_header_end: int  # bytes [0, ebml_header_end) are copied as-is
    segment: Header
    timestamp_scale: int
    duration: float  # Info/Duration in TimestampScale units, 0 when absent
    pre_cluster: list[Header]  # top-level elements before the first cluster, file order
    post_cluster: list[Header]  # top-level elements after the cluster region, file order
    first_cluster: int  # absolute offset
    clusters_end: int  # absolute offset (exclusive)
    tracks_payload: bytes  # children of Tracks, raw
    track_numbers: frozenset[int]
    cues: list[CuePoint] = field(default_factory=list)
    # bytes of a leading CRC-32 child inside Attachments (dropped when we append files to it)
    attachments_crc_len: int = 0

    def element(self, eid: int) -> Header | None:
        return next((h for h in self.pre_cluster + self.post_cluster if h.id == eid), None)

    @property
    def segment_data(self) -> int:
        return self.segment.data_offset

    def cluster_positions(self) -> list[tuple[int, int]]:
        """Sorted unique (absolute cluster offset, earliest cue time) pairs from the Cues."""
        best: dict[int, int] = {}
        for cp in self.cues:
            for _track, rel in cp.positions:
                pos = self.segment_data + rel
                if self.first_cluster <= pos < self.clusters_end:
                    best[pos] = min(cp.time, best.get(pos, cp.time))
        return sorted(best.items())


async def _header_at(read: Reader, offset: int) -> Header:
    buf = await read(offset, 12)
    return read_header(buf, 0, offset)


async def _payload(read: Reader, h: Header, limit: int) -> bytes:
    if h.size == UNKNOWN or h.size > limit:
        raise MkvError(f"element {h.id:#x} too large or of unknown size")
    data = await read(h.data_offset, h.size)
    if len(data) != h.size:
        raise MkvError(f"truncated element {h.id:#x}")
    return data


def _parse_seekhead(payload: bytes) -> list[tuple[int, int]]:
    out = []
    for seek in iter_children(payload):
        if seek.id != ID.SEEK:
            continue
        sid = pos = None
        for c in iter_children(payload, seek.data_offset, seek.end):
            value = payload[c.data_offset : c.end]
            if c.id == ID.SEEK_ID:
                sid = int.from_bytes(value, "big")
            elif c.id == ID.SEEK_POSITION:
                pos = read_uint(value)
        if sid is not None and pos is not None:
            out.append((sid, pos))
    return out


def _parse_cues(payload: bytes) -> list[CuePoint]:
    cues = []
    for cp in iter_children(payload):
        if cp.id != ID.CUE_POINT:
            continue
        time, positions = None, []
        for c in iter_children(payload, cp.data_offset, cp.end):
            if c.id == ID.CUE_TIME:
                time = read_uint(payload[c.data_offset : c.end])
            elif c.id == ID.CUE_TRACK_POSITIONS:
                track = cluster = None
                for t in iter_children(payload, c.data_offset, c.end):
                    if t.id == ID.CUE_TRACK:
                        track = read_uint(payload[t.data_offset : t.end])
                    elif t.id == ID.CUE_CLUSTER_POSITION:
                        cluster = read_uint(payload[t.data_offset : t.end])
                if track is not None and cluster is not None:
                    positions.append((track, cluster))
        if time is not None and positions:
            cues.append(CuePoint(time, tuple(positions)))
    return cues


def _track_numbers(payload: bytes) -> frozenset[int]:
    nums = set()
    for entry in iter_children(payload):
        if entry.id == ID.TRACK_ENTRY:
            for c in iter_children(payload, entry.data_offset, entry.end):
                if c.id == ID.TRACK_NUMBER:
                    nums.add(read_uint(payload[c.data_offset : c.end]))
    return frozenset(nums)


async def read_index(read: Reader, file_size: int) -> MkvIndex:
    try:
        return await _read_index(read, file_size)
    except EbmlError as e:
        raise MkvError(str(e)) from e


async def _read_index(read: Reader, file_size: int) -> MkvIndex:
    ebml = await _header_at(read, 0)
    if ebml.id != ID.EBML:
        raise MkvError("not an EBML file")
    segment = await _header_at(read, ebml.end)
    if segment.id != ID.SEGMENT:
        raise MkvError("no Segment after the EBML header")
    seg_end = file_size if segment.size == UNKNOWN else min(segment.end, file_size)

    # 1. walk the top-level elements up to the first Cluster
    pre: list[Header] = []
    pos = segment.data_offset
    first_cluster = None
    while pos < seg_end and len(pre) < MAX_HEADER_ELEMENTS:
        h = await _header_at(read, pos)
        if h.id == ID.CLUSTER:
            first_cluster = pos
            break
        if h.size == UNKNOWN:
            raise MkvError(f"top-level element {h.id:#x} of unknown size")
        pre.append(h)
        pos = h.end
    if first_cluster is None:
        raise MkvError("no Cluster found")

    # 2. follow SeekHeads to the elements stored after the clusters (Cues, Tags, another SeekHead)
    post: dict[int, Header] = {}
    pending = [h for h in pre if h.id == ID.SEEKHEAD]
    seen_seekheads: set[int] = set()
    while pending:
        sh = pending.pop()
        if sh.offset in seen_seekheads:
            continue
        seen_seekheads.add(sh.offset)
        for sid, rel in _parse_seekhead(await _payload(read, sh, MAX_SMALL_ELEMENT)):
            target = segment.data_offset + rel
            if target < first_cluster or target >= seg_end or target in post or sid == ID.CLUSTER:
                continue
            h = await _header_at(read, target)
            if h.id != sid or h.size == UNKNOWN:
                continue  # stale SeekHead entry
            post[target] = h
            if h.id == ID.SEEKHEAD:
                pending.append(h)
    post_cluster = sorted(post.values(), key=lambda h: h.offset)
    clusters_end = post_cluster[0].offset if post_cluster else seg_end

    # 3. small metadata we need to read
    info = next((h for h in pre if h.id == ID.INFO), None)
    tracks = next((h for h in pre if h.id == ID.TRACKS), None)
    if info is None or tracks is None:
        raise MkvError("Info or Tracks missing before the first Cluster")
    info_payload = await _payload(read, info, MAX_SMALL_ELEMENT)
    scale, duration = 1_000_000, 0.0
    for c in iter_children(info_payload):
        if c.id == ID.TIMESTAMP_SCALE:
            scale = read_uint(info_payload[c.data_offset : c.end]) or scale
        elif c.id == ID.DURATION:
            duration = read_float(info_payload[c.data_offset : c.end])
    tracks_payload = await _payload(read, tracks, MAX_SMALL_ELEMENT)

    index = MkvIndex(
        file_size=file_size,
        ebml_header_end=ebml.end,
        segment=segment,
        timestamp_scale=scale,
        duration=duration,
        pre_cluster=pre,
        post_cluster=post_cluster,
        first_cluster=first_cluster,
        clusters_end=clusters_end,
        tracks_payload=tracks_payload,
        track_numbers=_track_numbers(tracks_payload),
    )
    attachments = index.element(ID.ATTACHMENTS)
    if attachments is not None and attachments.size > 0:
        first = await _header_at(read, attachments.data_offset)
        if first.id == ID.CRC32:
            index.attachments_crc_len = first.total_size
    cues = index.element(ID.CUES)
    if cues is not None:
        index.cues = _parse_cues(await _payload(read, cues, MAX_CUES_BYTES))
    return index
