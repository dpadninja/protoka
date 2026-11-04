"""Serve a `VirtualLayout` byte range.

    Generated  bytes held in memory (headers, subtitle clusters, Cues)
    Original   bytes of a source file, streamed as they are (video clusters)
    Patched    a whole sidecar span read from its source, track numbers rewritten (patch.py), then sliced;
               the last few patched spans are kept, since players re-read around seek points

Sources are addressed by key ("video", "file:<idx>", "local:<path>"); `read(key, start, end)` streams
bytes [start, end] of that source — FileStream over the torrent in production, a plain file in tests.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable

from protoka.mux.layout import Generated, Original, Patched, VirtualLayout
from protoka.mux.patch import patch_clusters

SourceRead = Callable[[str, int, int], AsyncIterator[bytes]]
PATCHED_CACHE_SPANS = 8


class VirtualFile:
    def __init__(self, layout: VirtualLayout, read: SourceRead):
        self.layout = layout
        self.read = read
        self._patched: OrderedDict[Patched, bytes] = OrderedDict()
        self._patching: dict[Patched, asyncio.Future[bytes]] = {}

    @property
    def size(self) -> int:
        return self.layout.size

    async def _patched_span(self, chunk: Patched) -> bytes:
        cached = self._patched.get(chunk)
        if cached is not None:
            self._patched.move_to_end(chunk)
            return cached
        pending = self._patching.get(chunk)
        if pending is not None:  # another request is already reading this span
            return await asyncio.shield(pending)
        fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._patching[chunk] = fut
        try:
            raw = b"".join(
                [b async for b in self.read(chunk.source, chunk.src, chunk.src + chunk.length - 1)]
            )
            data = await asyncio.to_thread(patch_clusters, raw, dict(chunk.track_map))
            fut.set_result(data)
        except asyncio.CancelledError:
            fut.cancel()  # a cancellation cannot be stored as an exception; waiters see it as cancelled
            raise
        except Exception as e:
            fut.set_exception(e)
            fut.exception()  # mark retrieved: other waiters (if any) get it via shield
            raise
        finally:
            self._patching.pop(chunk, None)
        self._patched[chunk] = data
        while len(self._patched) > PATCHED_CACHE_SPANS:
            self._patched.popitem(last=False)
        return data

    async def iter_range(self, start: int, end: int) -> AsyncIterator[bytes]:
        """Bytes [start, end] (inclusive) of the virtual file."""
        lay = self.layout
        end = min(end, lay.size - 1)
        i = lay.locate(start)
        pos = start
        while pos <= end and i < len(lay.chunks):
            chunk, chunk_start = lay.chunks[i], lay.offsets[i]
            lo = pos - chunk_start
            hi = min(chunk.length, end - chunk_start + 1)  # exclusive, within the chunk
            if isinstance(chunk, Generated):
                yield chunk.data[lo:hi]
            elif isinstance(chunk, Original):
                async for data in self.read(chunk.source, chunk.src + lo, chunk.src + hi - 1):
                    yield data
            elif isinstance(chunk, Patched):
                yield (await self._patched_span(chunk))[lo:hi]
            pos = chunk_start + hi
            i += 1
