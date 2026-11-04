"""Build the byte layout of the virtual MKV: original video bytes + generated subtitle data. Pure.

Source:   EBML | Segment[ SeekHead Info Tracks …        | C0 C1 C2 … Cn | Cues Tags ]
Virtual:  EBML | Segment[ SeekHead' Info Tracks' Attach' | S0 C0 S1 C1 … Sn Cn | Tags Cues' ]

  EBML, Info, Chapters, Tags, C0…Cn  — copied byte for byte (`Original` chunks, read from the torrent)
  SeekHead'  new, points at the elements' new positions
  Tracks'    original track entries + one TrackEntry per subtitle track
  Attach'    original attachments (if any) + the fonts
  Si         generated clusters with the subtitle events that start in [time(Ci), time(Ci+1))
  Cues'      original cue points, cluster positions re-mapped; a cue for Ci points at Si, so after a
             seek the subtitles of that interval are read before the video they belong to.

Positions in SeekHead/Cues are written with a fixed 8-byte width, so sizes do not depend on positions:
the layout is computed once with placeholders and then filled in. Same input → same bytes, which matters
because a player reads the file through many independent Range requests.
"""

from __future__ import annotations

import bisect
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from protoka.mux import audio as audio_mod
from protoka.mux import ebml
from protoka.mux.audio import SidecarAudio
from protoka.mux.ebml import ID
from protoka.mux.mkv import MkvIndex
from protoka.subs.model import FontFace, SubtitleTrack

POSITION_WIDTH = 8
SIZE_WIDTH = 8
MAX_RELATIVE_TS = 32767


VIDEO = "video"


@dataclass(frozen=True)
class Original:
    src: int  # absolute offset in the source file
    length: int
    source: str = VIDEO  # which file: the video, or a sidecar ("file:<idx>" / "local:<path>")


@dataclass(frozen=True)
class Patched:
    """Sidecar clusters copied with their track numbers rewritten (same length, see patch.py)."""

    source: str
    src: int
    length: int
    track_map: tuple[tuple[int, int], ...]  # (old, new) pairs; a tuple keeps the chunk hashable


@dataclass(frozen=True)
class Generated:
    data: bytes

    @property
    def length(self) -> int:
        return len(self.data)


Chunk = Original | Generated | Patched


@dataclass
class VirtualLayout:
    chunks: list[Chunk]
    offsets: list[int]  # virtual offset of each chunk
    size: int
    summary: dict[str, Any] = field(default_factory=dict)

    def locate(self, offset: int) -> int:
        """Index of the chunk containing virtual `offset`."""
        return bisect.bisect_right(self.offsets, offset) - 1


def _uid(*parts: object) -> int:
    """Deterministic non-zero 63-bit UID (TrackUID / FileUID)."""
    digest = hashlib.sha1("\0".join(map(str, parts)).encode(), usedforsecurity=False).digest()
    return (int.from_bytes(digest[:8], "big") >> 1) or 1


def _ticks(ms: int, scale: int) -> int:
    return ms * 1_000_000 // scale


# -- element builders --------------------------------------------------------------------------
def track_entry(track: SubtitleTrack, number: int) -> bytes:
    children = [
        ebml.uint(ID.TRACK_NUMBER, number),
        ebml.uint(ID.TRACK_UID, _uid("track", track.file_idx, track.codec_id)),
        ebml.uint(ID.TRACK_TYPE, ebml.TRACK_TYPE_SUBTITLE),
        ebml.uint(ID.FLAG_LACING, 0),
        ebml.uint(ID.FLAG_DEFAULT, 0),
        ebml.string(ID.CODEC_ID, track.codec_id),
        ebml.string(ID.LANGUAGE, track.lang or "und"),
        ebml.string(ID.NAME, track.label),
    ]
    if track.codec_private:
        children.append(ebml.binary(ID.CODEC_PRIVATE, track.codec_private.encode("utf-8")))
    return ebml.master(ID.TRACK_ENTRY, *children)


def attached_file(face: FontFace) -> bytes:
    return ebml.master(
        ID.ATTACHED_FILE,
        ebml.string(ID.FILE_NAME, face.filename),
        ebml.string(ID.FILE_MEDIA_TYPE, face.mime),
        ebml.binary(ID.FILE_DATA, face.data),
        ebml.uint(ID.FILE_UID, _uid("font", face.id)),
    )


def _tracks_element(original_payload: bytes, entries: Sequence[bytes], *, clear_audio_default: bool) -> bytes:
    kept = audio_mod.video_entries(original_payload, clear_audio_default=clear_audio_default)
    return ebml.element(ID.TRACKS, kept + b"".join(entries))


def subtitle_clusters(events: Sequence[tuple[int, int, int, bytes]]) -> bytes:
    """Clusters for (start_ticks, duration_ticks, track_number, payload), sorted by start.

    A new cluster starts whenever a block's timestamp would not fit the int16 relative field."""
    out: list[bytes] = []
    cluster_ts: int | None = None
    blocks: list[bytes] = []

    def flush() -> None:
        if blocks and cluster_ts is not None:
            out.append(ebml.master(ID.CLUSTER, ebml.uint(ID.CLUSTER_TIMESTAMP, cluster_ts), *blocks))

    for start, duration, track, payload in events:
        if cluster_ts is None or start - cluster_ts > MAX_RELATIVE_TS:
            flush()
            cluster_ts, blocks = start, []
        block = ebml.element(ID.BLOCK, ebml.block_header(track, start - cluster_ts) + payload)
        blocks.append(ebml.master(ID.BLOCK_GROUP, block, ebml.uint(ID.BLOCK_DURATION, max(1, duration))))
    flush()
    return b"".join(out)


def _seekhead(entries: Sequence[tuple[int, int]]) -> bytes:
    return ebml.master(
        ID.SEEKHEAD,
        *(
            ebml.master(
                ID.SEEK,
                ebml.binary(ID.SEEK_ID, ebml.encode_id(eid)),
                ebml.uint(ID.SEEK_POSITION, pos, POSITION_WIDTH),
            )
            for eid, pos in entries
        ),
    )


def _cues(points: Sequence[tuple[int, Sequence[tuple[int, int]]]]) -> bytes:
    return ebml.master(
        ID.CUES,
        *(
            ebml.master(
                ID.CUE_POINT,
                ebml.uint(ID.CUE_TIME, time),
                *(
                    ebml.master(
                        ID.CUE_TRACK_POSITIONS,
                        ebml.uint(ID.CUE_TRACK, track),
                        ebml.uint(ID.CUE_CLUSTER_POSITION, pos, POSITION_WIDTH),
                    )
                    for track, pos in positions
                ),
            )
            for time, positions in points
        ),
    )


# -- the layout ----------------------------------------------------------------------------------
def _bucket_events(
    index: MkvIndex, numbered: Sequence[tuple[int, SubtitleTrack]], boundaries: Sequence[tuple[int, int]]
) -> dict[int, list[tuple[int, int, int, bytes]]]:
    """Subtitle events grouped by the cue-cluster they are inserted in front of (key: cluster offset)."""
    times = []
    running = 0
    for _pos, t in boundaries:  # Cues should be monotonic; guard against odd files
        running = max(running, t)
        times.append(running)
    buckets: dict[int, list[tuple[int, int, int, bytes]]] = {pos: [] for pos, _ in boundaries}
    for number, track in numbered:
        for ev in track.events:
            start = _ticks(ev.start_ms, index.timestamp_scale)
            duration = _ticks(ev.end_ms - ev.start_ms, index.timestamp_scale)
            k = max(0, bisect.bisect_right(times, start) - 1)
            buckets[boundaries[k][0]].append((start, duration, number, ev.payload.encode("utf-8")))
    for evs in buckets.values():
        evs.sort(key=lambda e: (e[0], e[2]))
    return buckets


class LayoutError(ValueError):
    pass


def _bucket_spans(
    boundaries: Sequence[tuple[int, int]], sidecars: Sequence[tuple[SidecarAudio, dict[int, int]]]
) -> dict[int, list[Patched]]:
    """Sidecar spans grouped by the video cue-cluster they are inserted in front of."""
    times = []
    running = 0
    for _pos, t in boundaries:
        running = max(running, t)
        times.append(running)
    buckets: dict[int, list[tuple[int, int, Patched]]] = {pos: [] for pos, _ in boundaries}
    for order, (sidecar, mapping) in enumerate(sidecars):
        pairs = tuple(sorted(mapping.items()))
        for span in audio_mod.spans(sidecar.index):
            k = max(0, bisect.bisect_right(times, span.time) - 1)
            chunk = Patched(sidecar.source, span.src, span.length, pairs)
            buckets[boundaries[k][0]].append((span.time, order, chunk))
    return {
        pos: [c for _t, _o, c in sorted(items, key=lambda x: (x[0], x[1]))] for pos, items in buckets.items()
    }


def build(
    index: MkvIndex,
    tracks: Sequence[SubtitleTrack],
    fonts: Sequence[FontFace],
    audio: Sequence[SidecarAudio] = (),
) -> VirtualLayout:
    """Track numbers: the video's own, then each sidecar's tracks, then the subtitle tracks."""
    boundaries = index.cluster_positions()
    if not boundaries:
        raise LayoutError("no usable Cues: cannot place subtitle clusters (needs the ffmpeg path)")

    next_number = max(index.track_numbers, default=0) + 1
    sidecars: list[tuple[SidecarAudio, dict[int, int]]] = []
    entries: list[bytes] = []
    for sidecar in audio:
        mapping = audio_mod.track_map(sidecar.index, next_number)
        sidecars.append((sidecar, mapping))
        entries += audio_mod.entries(sidecar, mapping)
        next_number += len(mapping)
    numbered = [(next_number + i, t) for i, t in enumerate(tracks)]
    entries += [track_entry(t, n) for n, t in numbered]
    clear_default = any(s.default for s in audio)
    attached = [attached_file(f) for f in fonts]
    buckets = _bucket_events(index, numbered, boundaries)
    span_buckets = _bucket_spans(boundaries, sidecars)

    # 1. top-level children of the new Segment, in order: (element id | None, [chunks])
    children: list[tuple[int | None, list[Chunk]]] = []
    has_attachments = index.element(ID.ATTACHMENTS) is not None
    for h in index.pre_cluster:
        if h.id in (ID.SEEKHEAD, ID.VOID, ID.CRC32, ID.CUES):
            continue
        if h.id == ID.TRACKS:
            tracks_el = _tracks_element(index.tracks_payload, entries, clear_audio_default=clear_default)
            children.append((ID.TRACKS, [Generated(tracks_el)]))
            if attached and not has_attachments:
                children.append((ID.ATTACHMENTS, [Generated(ebml.master(ID.ATTACHMENTS, *attached))]))
            continue
        if h.id == ID.ATTACHMENTS and attached:
            kept = Original(h.data_offset + index.attachments_crc_len, h.size - index.attachments_crc_len)
            extra = b"".join(attached)
            head = ebml.encode_id(ID.ATTACHMENTS) + ebml.encode_size(kept.length + len(extra), SIZE_WIDTH)
            children.append((ID.ATTACHMENTS, [Generated(head), kept, Generated(extra)]))
            continue
        children.append((h.id, [Original(h.offset, h.total_size)]))

    # 2. the cluster region, with subtitle clusters in front of every cue cluster
    region: list[Chunk] = []
    group_of: dict[int, int] = {}  # source cluster offset -> index in `region` where its group starts
    positions = [p for p, _ in boundaries]
    if positions[0] > index.first_cluster:
        region.append(Original(index.first_cluster, positions[0] - index.first_cluster))
    inserted = 0
    for k, pos in enumerate(positions):
        end = positions[k + 1] if k + 1 < len(positions) else index.clusters_end
        group_of[pos] = len(region)
        region.extend(span_buckets[pos])  # sidecar audio of [t(Ci), t(Ci+1)), copied lazily
        data = subtitle_clusters(buckets[pos])
        if data:
            region.append(Generated(data))
        if data or span_buckets[pos]:
            inserted += 1
        region.append(Original(pos, end - pos))
    children.append((None, region))

    for h in index.post_cluster:
        if h.id not in (ID.CUES, ID.SEEKHEAD, ID.VOID):
            children.append((h.id, [Original(h.offset, h.total_size)]))

    # Cue points: original times; cluster positions filled in after the offsets are known
    cue_points = [
        (
            cp.time,
            [
                (track, index.segment_data + rel)
                for track, rel in cp.positions
                if index.segment_data + rel in group_of
            ],
        )
        for cp in index.cues
    ]
    cue_points = [(t, p) for t, p in cue_points if p]
    cue_points.sort(key=lambda c: c[0])
    children.append((ID.CUES, [Generated(_cues([(t, [(tr, 0) for tr, _ in p]) for t, p in cue_points]))]))

    seek_ids: list[int] = []
    for eid, _ in children:
        if eid in (ID.INFO, ID.TRACKS, ID.ATTACHMENTS, ID.CHAPTERS, ID.TAGS, ID.CUES) and eid not in seek_ids:
            seek_ids.append(eid)
    seekhead_size = len(_seekhead([(eid, 0) for eid in seek_ids]))

    # 3. offsets (relative to the new segment data start)
    rel = seekhead_size
    child_pos: dict[int, int] = {}
    region_pos: list[int] = []
    for eid, element_chunks in children:
        if eid is not None and eid not in child_pos:
            child_pos[eid] = rel
        for c in element_chunks:
            if eid is None:
                region_pos.append(rel)
            rel += c.length
    segment_data_len = rel

    # 4. fill in positions
    def new_cluster_pos(src: int) -> int:
        return region_pos[group_of[src]]

    seekhead = _seekhead([(eid, child_pos[eid]) for eid in seek_ids])
    cues = _cues([(t, [(tr, new_cluster_pos(src)) for tr, src in p]) for t, p in cue_points])
    if len(seekhead) != seekhead_size or len(cues) != children[-1][1][0].length:
        # fixed-width positions guarantee this; if it ever breaks, refuse rather than emit a corrupt file
        raise LayoutError("rebuilt SeekHead/Cues changed size")
    children[-1] = (ID.CUES, [Generated(cues)])

    segment_header = ebml.encode_id(ID.SEGMENT) + ebml.encode_size(segment_data_len, SIZE_WIDTH)
    chunks: list[Chunk] = [Original(0, index.ebml_header_end), Generated(segment_header), Generated(seekhead)]
    for _eid, cs in children:
        chunks.extend(cs)
    chunks = [c for c in chunks if c.length > 0]
    offsets, pos = [], 0
    for c in chunks:
        offsets.append(pos)
        pos += c.length

    summary = {
        "kind": "virtual-mkv",
        "size": pos,
        "sourceSize": index.file_size,
        "insertedClusterGroups": inserted,
        "cuePoints": len(cue_points),
        "tracks": [
            {
                "number": n,
                "codec": t.codec_id,
                "lang": t.lang,
                "label": t.label,
                "events": len(t.events),
                "fileIdx": t.file_idx,
            }
            for n, t in numbered
        ],
        "audio": [
            {
                "numbers": sorted(m.values()),
                "lang": s.lang,
                "label": s.label,
                "default": s.default,
                "fileIdx": s.file_idx,
                "source": "torrent" if s.source.startswith("file:") else "repacked",
            }
            for s, m in sidecars
        ],
        "attachments": [f.filename for f in fonts],
    }
    return VirtualLayout(chunks=chunks, offsets=offsets, size=pos, summary=summary)
