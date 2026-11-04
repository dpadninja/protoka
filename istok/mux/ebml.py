"""EBML (the binary format under Matroska): variable-length integers and elements. Pure.

An element is  ID (1–4 bytes, marker bits kept) | size (vint, 1–8 bytes) | payload.
A size whose value bits are all ones means "unknown size" (live streams, some muxers' Segment/Cluster).
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass

UNKNOWN = -1


class EbmlError(ValueError):
    pass


# Matroska element IDs used here.
class ID:
    EBML = 0x1A45DFA3
    SEGMENT = 0x18538067
    SEEKHEAD = 0x114D9B74
    SEEK = 0x4DBB
    SEEK_ID = 0x53AB
    SEEK_POSITION = 0x53AC
    INFO = 0x1549A966
    TIMESTAMP_SCALE = 0x2AD7B1
    DURATION = 0x4489
    TRACKS = 0x1654AE6B
    TRACK_ENTRY = 0xAE
    TRACK_NUMBER = 0xD7
    TRACK_UID = 0x73C5
    TRACK_TYPE = 0x83
    FLAG_DEFAULT = 0x88
    FLAG_FORCED = 0x55AA
    FLAG_LACING = 0x9C
    CODEC_ID = 0x86
    CODEC_PRIVATE = 0x63A2
    LANGUAGE = 0x22B59C
    LANGUAGE_BCP47 = 0x22B59D
    NAME = 0x536E
    CLUSTER = 0x1F43B675
    CLUSTER_TIMESTAMP = 0xE7
    SIMPLE_BLOCK = 0xA3
    BLOCK_GROUP = 0xA0
    BLOCK = 0xA1
    BLOCK_DURATION = 0x9B
    CUES = 0x1C53BB6B
    CUE_POINT = 0xBB
    CUE_TIME = 0xB3
    CUE_TRACK_POSITIONS = 0xB7
    CUE_TRACK = 0xF7
    CUE_CLUSTER_POSITION = 0xF1
    ATTACHMENTS = 0x1941A469
    ATTACHED_FILE = 0x61A7
    FILE_NAME = 0x466E
    FILE_MEDIA_TYPE = 0x4660
    FILE_DATA = 0x465C
    FILE_UID = 0x46AE
    CHAPTERS = 0x1043A770
    TAGS = 0x1254C367
    VOID = 0xEC
    CRC32 = 0xBF


TRACK_TYPE_AUDIO = 0x02
TRACK_TYPE_SUBTITLE = 0x11


@dataclass(frozen=True)
class Header:
    id: int
    offset: int  # absolute offset of the element's first byte
    header_size: int
    size: int  # payload size, or UNKNOWN

    @property
    def data_offset(self) -> int:
        return self.offset + self.header_size

    @property
    def end(self) -> int:
        if self.size == UNKNOWN:
            raise EbmlError(f"element {self.id:#x} at {self.offset} has unknown size")
        return self.data_offset + self.size

    @property
    def total_size(self) -> int:
        return self.header_size + self.size


def _vint_length(first: int) -> int:
    for n in range(8):
        if first & (0x80 >> n):
            return n + 1
    raise EbmlError("invalid vint (zero first byte)")


def read_id(buf: bytes, pos: int) -> tuple[int, int]:
    if pos >= len(buf):
        raise EbmlError("truncated ID")
    n = _vint_length(buf[pos])
    if n > 4 or pos + n > len(buf):
        raise EbmlError("invalid or truncated ID")
    return int.from_bytes(buf[pos : pos + n], "big"), n


def read_size(buf: bytes, pos: int) -> tuple[int, int]:
    if pos >= len(buf):
        raise EbmlError("truncated size")
    n = _vint_length(buf[pos])
    if pos + n > len(buf):
        raise EbmlError("truncated size")
    value = int.from_bytes(buf[pos : pos + n], "big") & ((1 << (7 * n)) - 1)
    if value == (1 << (7 * n)) - 1:
        return UNKNOWN, n
    return value, n


def read_header(buf: bytes, pos: int, base: int = 0) -> Header:
    """Element header at `buf[pos]`; `base` = absolute offset of `buf[0]` in the file."""
    eid, n1 = read_id(buf, pos)
    size, n2 = read_size(buf, pos + n1)
    return Header(eid, base + pos, n1 + n2, size)


def iter_children(buf: bytes, start: int = 0, end: int | None = None, base: int = 0) -> Iterator[Header]:
    """Children of a master element whose payload is `buf[start:end]` (all in memory)."""
    pos, end = start, len(buf) if end is None else end
    while pos < end:
        h = read_header(buf, pos, base)
        if h.size == UNKNOWN or h.data_offset - base + h.size > end:
            raise EbmlError(f"child {h.id:#x} at {h.offset} overruns its parent")
        yield h
        pos = h.end - base


def read_uint(payload: bytes) -> int:
    return int.from_bytes(payload, "big") if payload else 0


def read_float(payload: bytes) -> float:
    if len(payload) == 4:
        return float(struct.unpack(">f", payload)[0])
    if len(payload) == 8:
        return float(struct.unpack(">d", payload)[0])
    return 0.0


# -- encoding ------------------------------------------------------------------------------------
def encode_id(eid: int) -> bytes:
    return eid.to_bytes((eid.bit_length() + 7) // 8, "big")


def encode_size(n: int, length: int | None = None) -> bytes:
    """Size vint; `length` forces a width (so a later rewrite cannot change element sizes)."""
    if length is None:
        length = 1
        while n >= (1 << (7 * length)) - 1:
            length += 1
    if not 1 <= length <= 8 or n >= (1 << (7 * length)) - 1:
        raise EbmlError(f"size {n} does not fit {length} bytes")
    return (n | (1 << (7 * length))).to_bytes(length, "big")


def element(eid: int, payload: bytes, size_length: int | None = None) -> bytes:
    return encode_id(eid) + encode_size(len(payload), size_length) + payload


def master(eid: int, *children: bytes) -> bytes:
    return element(eid, b"".join(children))


def uint(eid: int, value: int, width: int | None = None) -> bytes:
    """Unsigned integer element; `width` fixes the payload size (positions rewritten later)."""
    if width is None:
        width = max(1, (value.bit_length() + 7) // 8)
    return element(eid, value.to_bytes(width, "big"))


def string(eid: int, value: str) -> bytes:
    return element(eid, value.encode("utf-8"))


def binary(eid: int, value: bytes) -> bytes:
    return element(eid, value)


def block_header(track_number: int, relative_ts: int, flags: int = 0) -> bytes:
    """Block/SimpleBlock header: track number vint, int16 timestamp relative to the cluster, flags."""
    if not -32768 <= relative_ts <= 32767:
        raise EbmlError(f"relative timestamp {relative_ts} out of int16 range")
    return encode_size(track_number) + struct.pack(">hB", relative_ts, flags)
