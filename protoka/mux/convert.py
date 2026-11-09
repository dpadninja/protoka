"""Repack an external audio file into MKA with ffmpeg (`-c copy`, nothing re-encoded).

Needed for raw streams (.ac3, .dts, .flac, …: no clusters or timestamps to copy) and for MKA files that
cannot be copied as they are (no Cues, another TimestampScale). ffmpeg reads the source through our own
HTTP endpoint (`?mux=0`), so the torrent downloads it at top priority, and writes
`<torrent cache>/converted/<file idx>.mka`. One task per output file; it is not tied to a request or to
the engine, so a repack that misses the wait limit keeps going and the next playback finds the file.
At most MUX_REPACK_CONCURRENCY repacks run at once (PriorityGate); the one for the episode being opened
goes before background (prefetch) repacks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path

from protoka import logfmt
from protoka.mux.ffmpeg import AUDIO_DEMUXERS, ffmpeg_binary, limit, url_input

log = logging.getLogger(__name__)

READ_CHUNK = 1024 * 1024
URGENT, BACKGROUND = 0, 1  # repack priorities: the file being watched first, prefetch after


class PriorityGate:
    """At most `limit` holders at a time; waiters are admitted by (priority, arrival).

    `promote(key)` raises a waiting entry to URGENT — a queued background repack that the player now
    needs must not wait behind other background work."""

    def __init__(self, limit: int = 1):
        self.limit = max(1, limit)
        self.active = 0
        self._waiting: dict[Path, list] = {}  # key -> [priority, seq, future]
        self._seq = 0

    def _admit(self) -> None:
        while self.active < self.limit and self._waiting:
            key, entry = min(self._waiting.items(), key=lambda kv: (kv[1][0], kv[1][1]))
            del self._waiting[key]
            self.active += 1
            if not entry[2].done():
                entry[2].set_result(None)
            else:  # the waiter went away meanwhile
                self.active -= 1

    async def acquire(self, key: Path, priority: int) -> None:
        self._seq += 1
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiting[key] = [priority, self._seq, fut]
        self._admit()
        try:
            await fut
        except asyncio.CancelledError:
            if self._waiting.get(key, [None, None, None])[2] is fut:
                del self._waiting[key]
            elif fut.done() and not fut.cancelled():
                self.release()  # admitted just before the cancellation landed
            raise

    def release(self) -> None:
        self.active -= 1
        self._admit()

    def promote(self, key: Path) -> None:
        entry = self._waiting.get(key)
        if entry is not None:
            entry[0] = URGENT

    def queued(self) -> int:
        return len(self._waiting)


_gate = PriorityGate(1)
_tasks: dict[Path, asyncio.Task[Path]] = {}


def set_concurrency(limit: int) -> None:
    """MUX_REPACK_CONCURRENCY: how many ffmpeg repacks may run at once (1 by default)."""
    _gate.limit = max(1, int(limit))
    _gate._admit()


def converted_path(save_path: Path, file_idx: int) -> Path:
    return Path(save_path) / "converted" / f"{file_idx}.mka"


def ensure_mka(source_url: str, out: Path, *, urgent: bool = True, tag: str = "") -> asyncio.Task[Path]:
    """The repack task for `out` (started if needed; already finished if the file exists).

    `urgent`: the file is needed for the episode being opened now (vs. prefetching the next one).
    `tag`: how log lines name the file, e.g. "[Show · Title - 01.ac3]"."""
    task = _tasks.get(out)
    tag = tag or f"[{out.name}]"
    if task is not None and not (task.done() and (task.cancelled() or task.exception())):
        if urgent and out in _gate._waiting and _gate._waiting[out][0] != URGENT:
            _gate.promote(out)
            log.info("%s audio: repack moved to the front of the queue (needed now)", tag)
        return task
    task = asyncio.create_task(_queued_repack(source_url, out, URGENT if urgent else BACKGROUND, tag))
    _tasks[out] = task
    return task


async def _queued_repack(source_url: str, out: Path, priority: int, tag: str) -> Path:
    if await asyncio.to_thread(out.exists):
        log.debug("%s audio: already repacked (%s)", tag, out)
        return out
    ahead = _gate.active + _gate.queued()
    if ahead >= _gate.limit:
        kind = "needed now" if priority == URGENT else "background"
        log.info("%s audio: repack queued (%d ahead, %s)", tag, ahead - _gate.limit + 1, kind)
    await _gate.acquire(out, priority)
    try:
        return await _repack(source_url, out, tag)
    finally:
        _gate.release()


async def _repack(source_url: str, out: Path, tag: str = "") -> Path:
    if await asyncio.to_thread(out.exists):
        return out
    binary = ffmpeg_binary()
    if binary is None:
        raise FileNotFoundError("ffmpeg is not installed")
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    cmd = [binary, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           *url_input(source_url, AUDIO_DEMUXERS),
           "-map", "0:a", "-c", "copy", "-f", "matroska",
           "-cluster_time_limit", "2000", str(part)]  # fmt: skip
    log.info("%s audio: repack started (ffmpeg -c copy → .mka)", tag)
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    limit(proc)
    try:
        _, err = await proc.communicate()
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    if proc.returncode:
        part.unlink(missing_ok=True)
        lines = err.decode(errors="replace").strip().splitlines() or [""]
        detail = lines[0] if len(lines) == 1 else f"{lines[0]} … {lines[-1]}"  # the cause, then the outcome
        reason = f"ffmpeg exited with {proc.returncode}: {detail[:500]}"
        log.warning("%s audio: repack failed: %s", tag, reason)
        raise RuntimeError(reason)
    os.replace(part, out)
    took = logfmt.secs(time.monotonic() - started)
    size = (await asyncio.to_thread(out.stat)).st_size
    log.info("%s audio: repack done in %s, %s", tag, took, logfmt.size(size))
    return out


async def read_local(path: Path, start: int, end: int) -> AsyncIterator[bytes]:
    """Bytes [start, end] of a local file, read off the event loop."""

    def chunk(offset: int, n: int) -> bytes:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(n)

    pos = start
    while pos <= end:
        data = await asyncio.to_thread(chunk, pos, min(READ_CHUNK, end - pos + 1))
        if not data:
            break
        yield data
        pos += len(data)
