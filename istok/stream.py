"""Byte-range reads of a torrent file with a sliding window of piece deadlines.

Upstream: torrent-stream fileStream + engine.select/critical. Here: libtorrent set_piece_deadline
on the next N pieces ahead of the read position; data is read through libtorrent (read_piece), so it
is correct regardless of the disk-I/O backend's write-back caching.
"""

from __future__ import annotations

import math
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

from istok.engine import Engine, Selection

READAHEAD_BYTES = 24 * 1024 * 1024
MIN_READAHEAD_PIECES = 4
MAX_READAHEAD_PIECES = 64
DEADLINE_STEP_MS = 100
CHUNK = 256 * 1024

_RANGE_RE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", re.IGNORECASE)


class RangeNotSatisfiable(ValueError):
    pass


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int  # inclusive

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def parse_range(header: str | None, size: int) -> ByteRange | None:
    """First range of a `Range: bytes=…` header. None → serve the whole file; raises if unsatisfiable.

    Upstream passed rangeParser's error codes (-1/-2) straight into the 206 branch.
    Multiple ranges: only the first is served (as upstream).
    """
    if not header:
        return None
    unit, _, spec = header.partition("=")
    m = _RANGE_RE.match(f"{unit}={spec.split(',', 1)[0]}")
    if not m:
        return None  # malformed → ignore and serve 200 (RFC 9110 §14.2)
    s, e = m.group(1), m.group(2)
    if s == "" and e == "":
        return None
    if size == 0:
        raise RangeNotSatisfiable(header)
    if s == "":  # suffix range: last N bytes
        n = int(e)
        if n == 0:
            raise RangeNotSatisfiable(header)
        return ByteRange(max(0, size - n), size - 1)
    start = int(s)
    end = int(e) if e != "" else size - 1
    if start >= size or start > end:
        raise RangeNotSatisfiable(header)
    return ByteRange(start, min(end, size - 1))


class FileStream:
    """Async iterator over bytes [start, end] of file `file_idx`."""

    def __init__(self, engine: Engine, file_idx: int, start: int, end: int):
        files = engine.files
        assert files is not None
        self.engine = engine
        self.file_idx = file_idx
        self.file_offset = files[file_idx]["offset"]
        self.start = start
        self.end = end
        plen = engine.piece_length
        _, self.last_file_piece = engine.file_piece_range(file_idx)
        self.window = max(MIN_READAHEAD_PIECES, min(MAX_READAHEAD_PIECES, math.ceil(READAHEAD_BYTES / plen)))
        self._held: set[int] = set()
        self._window_start = -1
        self.selection = Selection(file_idx, (self.file_offset + start) // plen, self.last_file_piece)

    def _advance_window(self, piece: int) -> None:
        if piece == self._window_start:
            return
        self._window_start = piece
        wanted = range(piece, min(piece + self.window, self.last_file_piece + 1))
        drop = [p for p in self._held if p not in wanted]
        if drop:
            self.engine.release_deadlines(drop)
            self._held.difference_update(drop)
        # deadline grows with distance from the read position → the next piece is the most urgent
        new = [(p, i * DEADLINE_STEP_MS) for i, p in enumerate(wanted) if p not in self._held]
        self._held.update(self.engine.acquire_deadlines(new))
        self.selection.offset = piece - self.selection.from_piece

    async def __aiter__(self) -> AsyncIterator[bytes]:
        e = self.engine
        plen = e.piece_length
        pos = self.file_offset + self.start
        end_abs = self.file_offset + self.end
        e.selections.append(self.selection)
        try:
            while pos <= end_abs:
                piece = pos // plen
                self._advance_window(piece)
                data = await e.read_piece(piece)
                piece_start = piece * plen
                lo = pos - piece_start
                hi = min(len(data), end_abs - piece_start + 1)
                if hi <= lo:
                    raise RuntimeError(f"short piece {piece}: {len(data)} bytes")
                view = memoryview(data)[lo:hi]
                for i in range(0, len(view), CHUNK):
                    yield bytes(view[i : i + CHUNK])
                pos += hi - lo
        finally:
            self.close()

    def close(self) -> None:
        if self._held:
            self.engine.release_deadlines(list(self._held))
            self._held.clear()
        if self.selection in self.engine.selections:
            self.engine.selections.remove(self.selection)


async def read_range(engine: Engine, file_idx: int, start: int, end: int) -> bytes:
    """Bytes [start, end] (inclusive) of a torrent file, downloading the needed pieces first."""
    if end < start:
        return b""
    return b"".join([chunk async for chunk in FileStream(engine, file_idx, start, end)])


async def read_file(engine: Engine, file_idx: int, max_bytes: int) -> bytes:
    """A whole (small) torrent file: subtitles, fonts, font archives."""
    size = engine.files[file_idx]["length"]  # type: ignore[index]
    if size > max_bytes:
        raise ValueError(f"file {file_idx} is too large ({size} > {max_bytes} bytes)")
    return await read_range(engine, file_idx, 0, size - 1)
