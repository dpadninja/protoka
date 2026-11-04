"""Rewrite track numbers inside copied clusters of a sidecar MKA. Pure.

An external audio file has its own track numbers (usually 1). In the virtual MKV its track gets a new
number, so every block of it must say so. The number is the first byte of a Block/SimpleBlock payload
(a 1-byte vint for numbers < 127), so the change is in place and the cluster keeps its size — which is
what lets the virtual file's layout be computed before the audio is downloaded.

    Cluster ─┬─ CRC-32        recomputed after patching (ffmpeg and mkvmerge write one per cluster)
             ├─ Timestamp
             ├─ SimpleBlock   [track vint][int16 ts][flags][frames…]   ← track vint patched
             └─ BlockGroup ── Block [track vint]…                       ← track vint patched
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Mapping

from protoka.mux.ebml import ID, UNKNOWN, EbmlError, iter_children, read_header


class PatchError(ValueError):
    pass


def _patch_block(buf: bytearray, offset: int, track_map: Mapping[int, int]) -> None:
    first = buf[offset]
    if not first & 0x80:
        raise PatchError(f"multi-byte track number at {offset}")
    old = first & 0x7F
    new = track_map.get(old)
    if new is None:
        raise PatchError(f"block of unexpected track {old} at {offset}")
    if not 0 < new < 127:
        raise PatchError(f"new track number {new} does not fit one byte")
    buf[offset] = 0x80 | new


def _patch_cluster(buf: bytearray, start: int, end: int, track_map: Mapping[int, int]) -> None:
    """Patch blocks of one cluster whose payload is buf[start:end]; recompute its CRC-32 if present."""
    crc_at: int | None = None
    data = bytes(buf)  # header parsing works on a stable snapshot; patches go to `buf`
    for i, child in enumerate(iter_children(data, start, end)):
        if child.id == ID.CRC32:
            if i != 0 or child.size != 4:
                raise PatchError("CRC-32 must be the first child and 4 bytes long")
            crc_at = child.data_offset
        elif child.id == ID.SIMPLE_BLOCK:
            _patch_block(buf, child.data_offset, track_map)
        elif child.id == ID.BLOCK_GROUP:
            for g in iter_children(data, child.data_offset, child.end):
                if g.id == ID.BLOCK:
                    _patch_block(buf, g.data_offset, track_map)
    if crc_at is not None:
        # EBML: CRC-32 (IEEE, little-endian) of the parent's payload after the CRC-32 element
        crc = zlib.crc32(bytes(buf[crc_at + 4 : end])) & 0xFFFFFFFF
        buf[crc_at : crc_at + 4] = struct.pack("<I", crc)


def patch_clusters(data: bytes, track_map: Mapping[int, int]) -> bytes:
    """`data` = whole top-level elements (clusters, maybe Void) cut from a sidecar's cluster region."""
    buf = bytearray(data)
    pos = 0
    try:
        while pos < len(buf):
            h = read_header(data, pos)
            if h.size == UNKNOWN:
                raise PatchError("element of unknown size in a sidecar cluster region")
            if h.end > len(buf):
                raise PatchError(f"element at {pos} runs past the end of the span")
            if h.id == ID.CLUSTER:
                _patch_cluster(buf, h.data_offset, h.end, track_map)
            pos = h.end
    except EbmlError as e:
        raise PatchError(str(e)) from e
    return bytes(buf)
