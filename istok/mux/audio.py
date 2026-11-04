"""External audio (sidecar MKA) → pieces of the virtual MKV. Pure.

A sidecar is described by its own `MkvIndex` (read by `mkv.read_index`, the same code as for the video).
From it this module derives:
    check_compatible  can its clusters be copied as they are? (same TimestampScale, Cues, 1-byte numbers)
    entries           its TrackEntry elements, renumbered, with Language/Name/FlagDefault set
    spans             byte ranges between its cue clusters, each with its start time: the units that
                      `layout.build` interleaves with the video clusters
Anything incompatible raises AudioError; the service then repacks the file with ffmpeg and tries again.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from istok.mux import ebml
from istok.mux.ebml import ID
from istok.mux.mkv import MkvIndex

MAX_TRACK_NUMBER = 126  # 1-byte vint: lets blocks be renumbered in place (see patch.py)
DURATION_TOLERANCE = 0.05


class AudioError(ValueError):
    pass


@dataclass(frozen=True)
class SidecarAudio:
    source: str  # chunk source key for VirtualFile: "file:<idx>" (torrent) or "local:<path>" (repacked)
    file_idx: int  # torrent file the audio came from
    index: MkvIndex
    lang: str | None
    label: str
    default: bool = False  # make its (first) audio track the default one


@dataclass(frozen=True)
class Span:
    time: int  # start time of the first cluster, TimestampScale units
    src: int  # absolute offset in the sidecar file
    length: int


def _track_numbers_by_entry(payload: bytes) -> list[tuple[ebml.Header, int, int]]:
    """(entry header, track number, track type) for each TrackEntry of a Tracks payload."""
    out = []
    for entry in ebml.iter_children(payload):
        if entry.id != ID.TRACK_ENTRY:
            continue
        number = ttype = 0
        for c in ebml.iter_children(payload, entry.data_offset, entry.end):
            if c.id == ID.TRACK_NUMBER:
                number = ebml.read_uint(payload[c.data_offset : c.end])
            elif c.id == ID.TRACK_TYPE:
                ttype = ebml.read_uint(payload[c.data_offset : c.end])
        out.append((entry, number, ttype))
    return out


def check_compatible(video: MkvIndex, sidecar: MkvIndex) -> list[str]:
    """Raise AudioError if the sidecar's clusters cannot be copied; return soft warnings."""
    if sidecar.timestamp_scale != video.timestamp_scale:
        raise AudioError(f"TimestampScale {sidecar.timestamp_scale} != video {video.timestamp_scale}")
    if not sidecar.cluster_positions():
        raise AudioError("no Cues")
    numbers = [n for _e, n, _t in _track_numbers_by_entry(sidecar.tracks_payload)]
    if not numbers or any(not 0 < n <= MAX_TRACK_NUMBER for n in numbers):
        raise AudioError(f"unsupported track numbers {numbers}")
    warnings = []
    if video.duration and sidecar.duration:
        diff = abs(sidecar.duration - video.duration) / video.duration
        if diff > DURATION_TOLERANCE:
            warnings.append(f"duration differs from the video by {diff:.0%} (wrong episode or out of sync?)")
    return warnings


def track_map(sidecar: MkvIndex, first_number: int) -> dict[int, int]:
    """Old → new track numbers for all tracks of a sidecar, consecutive from `first_number`."""
    numbers = sorted(n for _e, n, _t in _track_numbers_by_entry(sidecar.tracks_payload))
    mapping = {old: first_number + i for i, old in enumerate(numbers)}
    if mapping and max(mapping.values()) > MAX_TRACK_NUMBER:
        raise AudioError("too many tracks for 1-byte track numbers")
    return mapping


def _uid(*parts: object) -> int:
    digest = hashlib.sha1("\0".join(map(str, parts)).encode()).digest()
    return (int.from_bytes(digest[:8], "big") >> 1) or 1


_REPLACED = (ID.TRACK_NUMBER, ID.TRACK_UID, ID.FLAG_DEFAULT, ID.CRC32)
_UNDEFINED_LANGS = ("", "und")  # what muxers write when nobody set a language


def entries(sidecar: SidecarAudio, mapping: dict[int, int]) -> list[bytes]:
    """The sidecar's TrackEntry elements for the virtual Tracks element.

    New TrackNumber and a deterministic TrackUID; Language/Name filled from the file name when the
    sidecar has none; FlagDefault=1 only on its first audio track and only if `sidecar.default`."""
    payload = sidecar.index.tracks_payload
    out = []
    default_given = False
    for entry, number, ttype in _track_numbers_by_entry(payload):
        children = list(ebml.iter_children(payload, entry.data_offset, entry.end))
        own_lang = {
            c.id: payload[c.data_offset : c.end].decode("ascii", "replace").strip("\0 ").lower()
            for c in children
            if c.id in (ID.LANGUAGE, ID.LANGUAGE_BCP47)
        }
        # the sidecar's own language wins, unless it is missing/"und": then the folder/name guess is used
        set_lang = bool(sidecar.lang) and all(v in _UNDEFINED_LANGS for v in own_lang.values())
        replaced = _REPLACED + ((ID.LANGUAGE, ID.LANGUAGE_BCP47) if set_lang else ())
        kept = b"".join(payload[c.offset : c.end] for c in children if c.id not in replaced)
        ids = {c.id for c in children}
        is_default = sidecar.default and ttype == ebml.TRACK_TYPE_AUDIO and not default_given
        default_given |= is_default
        extra = [
            ebml.uint(ID.TRACK_NUMBER, mapping[number]),
            ebml.uint(ID.TRACK_UID, _uid("sidecar", sidecar.file_idx, number)),
            ebml.uint(ID.FLAG_DEFAULT, 1 if is_default else 0),
        ]
        if set_lang and sidecar.lang:
            extra.append(ebml.string(ID.LANGUAGE, sidecar.lang))
        if ID.NAME not in ids:
            extra.append(ebml.string(ID.NAME, sidecar.label))
        out.append(ebml.master(ID.TRACK_ENTRY, *extra, kept))
    return out


def video_entries(tracks_payload: bytes, *, clear_audio_default: bool) -> bytes:
    """The video file's own TrackEntry elements (CRC-32 dropped); optionally FlagDefault=0 on its audio.

    In Matroska a missing FlagDefault means 1, so it has to be written explicitly."""
    out = []
    for entry, _number, ttype in _track_numbers_by_entry(tracks_payload):
        if not (clear_audio_default and ttype == ebml.TRACK_TYPE_AUDIO):
            out.append(tracks_payload[entry.offset : entry.end])
            continue
        children = ebml.iter_children(tracks_payload, entry.data_offset, entry.end)
        kept = b"".join(
            tracks_payload[c.offset : c.end] for c in children if c.id not in (ID.FLAG_DEFAULT, ID.CRC32)
        )
        out.append(ebml.master(ID.TRACK_ENTRY, kept, ebml.uint(ID.FLAG_DEFAULT, 0)))
    return b"".join(out)


def spans(index: MkvIndex) -> list[Span]:
    """The sidecar's cluster region cut at its cue clusters: [(time, offset, length)]."""
    boundaries = index.cluster_positions()
    out = []
    if boundaries and boundaries[0][0] > index.first_cluster:
        out.append(Span(0, index.first_cluster, boundaries[0][0] - index.first_cluster))
    for k, (pos, time) in enumerate(boundaries):
        end = boundaries[k + 1][0] if k + 1 < len(boundaries) else index.clusters_end
        out.append(Span(time, pos, end - pos))
    return [s for s in out if s.length > 0]
