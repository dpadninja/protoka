"""Per-torrent engine: wraps a libtorrent handle, waits for metadata/pieces, reads piece data.

Replaces upstream's torrent-stream fork + PeerSearch (analysis/02-peer-discovery.md).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import libtorrent as lt

if TYPE_CHECKING:
    from istok.session import TorrentSession

log = logging.getLogger(__name__)

PIECE_CACHE_BYTES = 64 * 1024 * 1024


class EngineError(Exception):
    pass


@dataclass
class SourceStats:
    """One entry of stats `sources[]` (upstream PeerSearch.stats()).

    Stremio parses this strictly (stremio-core `Source`): `lastStarted` must always be a string, so it
    starts at creation time — like upstream, where every source is started when the engine is created."""

    url: str
    numFound: int = 0
    numRequests: int = 0
    lastStarted: float = field(default_factory=time.time)
    lastError: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "numFound": self.numFound,
            # libtorrent dedups peers internally; per-source uniqueness is not exposed.
            "numFoundUniq": self.numFound,
            "numRequests": self.numRequests,
            "url": self.url,
            "lastStarted": _iso(self.lastStarted),
            "lastError": self.lastError,
        }


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int(ts % 1 * 1000):03d}Z"


@dataclass
class Selection:
    """An active read window (shown as `selections` in stats)."""

    file_idx: int
    from_piece: int
    to_piece: int
    offset: int = 0
    priority: int = 1

    def to_json(self) -> dict[str, Any]:
        return {
            "from": self.from_piece,
            "to": self.to_piece,
            "offset": self.offset,
            "priority": self.priority,
            "fileIdx": self.file_idx,
        }


@dataclass
class _Waiters:
    have: dict[int, list[asyncio.Future[None]]] = field(default_factory=dict)
    read: dict[int, asyncio.Future[bytes]] = field(default_factory=dict)


class Engine:
    def __init__(
        self,
        session: TorrentSession,
        info_hash: str,
        handle: lt.torrent_handle,
        save_path: Path,
        trackers: list[str],
        options: dict[str, Any],
    ):
        self.session = session
        self.info_hash = info_hash
        self.handle = handle
        self.save_path = save_path
        self.options = options
        self.created_at = time.time()
        self.metadata_ready = asyncio.Event()
        self.error: str | None = None
        self.destroyed = False
        self._loop = asyncio.get_running_loop()
        self._waiters = _Waiters()
        self._piece_cache: OrderedDict[int, bytes] = OrderedDict()
        self._piece_cache_bytes = 0
        self._deadline_refs: Counter[int] = Counter()
        self.selections: list[Selection] = []
        self.open_streams = 0
        self.inactive_handle: asyncio.TimerHandle | None = None
        self.sources: dict[str, SourceStats] = {t: SourceStats(f"tracker:{t}") for t in trackers}
        self.sources["dht"] = SourceStats(f"dht:{info_hash}")
        self._files: list[dict[str, Any]] | None = None
        # subtitle discovery (subs.service.SubtitleService) and mux results per file index,
        # created lazily by mux.service — kept here so they live and die with the torrent
        self.subs: Any = None
        self.mux_cache: dict[int, Any] = {}
        self._resume_future: asyncio.Future[bytes | None] | None = None
        self._resume_lock = asyncio.Lock()  # one save_resume_data at a time (periodic + shutdown)
        # library lifecycle (library/service.py): when it was last asked for / watched, what was opened
        self.last_activity: float = time.time()
        self.last_watched: float = time.time()
        self.opened_files: list[int] = []  # in opening order; the last one drives the prefetch
        self.state: str = "streaming"
        self.applied: tuple | None = None  # last settings applied to the handle by the library
        self.logged_opened: set[int] = set()  # files whose "stream: opened" line was already logged
        if handle.status().has_metadata:
            self._on_metadata()

    # -- metadata ---------------------------------------------------------------------------------
    @property
    def torrent_info(self) -> lt.torrent_info | None:
        return self.handle.torrent_file() if self.metadata_ready.is_set() else None

    @property
    def files(self) -> list[dict[str, Any]] | None:
        """parse-torrent style: [{path, name, length, offset}] in torrent order (pad files included)."""
        return self._files

    def _on_metadata(self) -> None:
        ti = self.handle.torrent_file()
        if ti is None:
            return
        fs = ti.layout()
        files = []
        for i in range(fs.num_files()):
            path = fs.file_path(i).replace("\\", "/")
            files.append(
                {
                    "path": path,
                    "name": fs.file_name(i),
                    "length": fs.file_size(i),
                    "offset": fs.file_offset(i),
                }
            )
        self._files = files
        self.piece_length = ti.piece_length()
        self.num_pieces = ti.num_pieces()
        self.name = ti.name()
        if not self.metadata_ready.is_set():
            self.metadata_ready.set()
            self.session.on_metadata(self)

    async def wait_metadata(self, timeout: float | None = None) -> None:
        if self.metadata_ready.is_set():
            return
        if self.destroyed:
            raise EngineError("engine destroyed")
        await asyncio.wait_for(self.metadata_ready.wait(), timeout)
        if self.destroyed:
            raise EngineError("engine destroyed")

    def file_piece_range(self, file_idx: int) -> tuple[int, int]:
        f = self._files[file_idx]  # type: ignore[index]
        start = f["offset"] // self.piece_length
        end = (f["offset"] + max(f["length"], 1) - 1) // self.piece_length
        return start, end

    # -- alerts -----------------------------------------------------------------------------------
    def on_alert(self, a: Any) -> None:
        name = type(a).__name__
        if name == "metadata_received_alert":
            self._on_metadata()
        elif name == "piece_finished_alert":
            self._on_have(a.piece_index)
        elif name == "read_piece_alert":
            self._on_read(a)
        elif name == "tracker_announce_alert":
            src = self._tracker_source(a)
            src.numRequests += 1
            src.lastStarted = time.time()
        elif name == "tracker_reply_alert":
            src = self._tracker_source(a)
            src.numFound += a.num_peers
            src.lastError = None
        elif name == "tracker_error_alert":
            # libtorrent announces once per listen socket; link-local/loopback sockets can never
            # reach a tracker and would bury the meaningful error.
            if _routable_endpoint(a):
                src = self._tracker_source(a)
                reason = a.failure_reason() or a.error.message()
                src.lastError = reason or _alert_message(a)
        elif name == "dht_reply_alert":
            src = self.sources["dht"]
            src.numRequests += 1
            src.numFound += a.num_peers
            src.lastStarted = time.time()
        elif name == "torrent_error_alert":
            self.error = _alert_message(a)
            log.error("Engine error for %s: %s", self.info_hash, self.error)
        elif name == "hash_failed_alert":
            log.warning("Engine invalid piece %s for %s", a.piece_index, self.info_hash)
        elif name == "save_resume_data_alert":
            if self._resume_future and not self._resume_future.done():
                try:
                    self._resume_future.set_result(bytes(lt.write_resume_data_buf(a.params)))
                except Exception as e:  # noqa: BLE001
                    self._resume_future.set_exception(e)
        elif name == "save_resume_data_failed_alert":
            if self._resume_future and not self._resume_future.done():
                self._resume_future.set_result(None)

    def _tracker_source(self, a: Any) -> SourceStats:
        url = getattr(a, "tracker_url", None) or getattr(a, "url", "") or ""
        if callable(url):
            url = url()
        src = self.sources.get(url)
        if src is None:
            src = self.sources[url] = SourceStats(f"tracker:{url}")
        return src

    def _on_have(self, piece: int) -> None:
        for fut in self._waiters.have.pop(piece, ()):
            if not fut.done():
                fut.set_result(None)
        if self._deadline_refs.get(piece):
            del self._deadline_refs[piece]

    def _on_read(self, a: Any) -> None:
        fut = self._waiters.read.pop(a.piece, None)
        if fut is None or fut.done():
            return
        if a.error.value() != 0:
            fut.set_exception(EngineError(f"read_piece {a.piece} failed: {a.error.message()}"))
        else:
            fut.set_result(bytes(a.buffer))

    # -- piece access ------------------------------------------------------------------------------
    def have_piece(self, piece: int) -> bool:
        return self.handle.have_piece(piece)

    async def wait_piece(self, piece: int) -> None:
        if self.have_piece(piece):
            return
        fut: asyncio.Future[None] = self._loop.create_future()
        self._waiters.have.setdefault(piece, []).append(fut)
        if self.have_piece(piece):  # finished between the check and registration
            self._on_have(piece)
        try:
            await fut
        finally:
            lst = self._waiters.have.get(piece)
            if lst and fut in lst:
                lst.remove(fut)
                if not lst:
                    del self._waiters.have[piece]

    async def read_piece(self, piece: int) -> bytes:
        cached = self._piece_cache.get(piece)
        if cached is not None:
            self._piece_cache.move_to_end(piece)
            return cached
        for _attempt in range(5):
            await self.wait_piece(piece)
            fut = self._waiters.read.get(piece)
            if fut is None:
                fut = self._loop.create_future()
                self._waiters.read[piece] = fut
                self.handle.read_piece(piece)
            try:
                data = await asyncio.shield(fut)
            except EngineError:
                # e.g. piece was evicted / failed hash after finishing; wait for it again
                await asyncio.sleep(0.2)
                continue
            self._cache_put(piece, data)
            return data
        raise EngineError(f"cannot read piece {piece}")

    def _cache_put(self, piece: int, data: bytes) -> None:
        if piece in self._piece_cache:
            return
        self._piece_cache[piece] = data
        self._piece_cache_bytes += len(data)
        while self._piece_cache_bytes > PIECE_CACHE_BYTES and len(self._piece_cache) > 1:
            _, old = self._piece_cache.popitem(last=False)
            self._piece_cache_bytes -= len(old)

    # -- prioritisation ----------------------------------------------------------------------------
    def acquire_deadlines(self, items: list[tuple[int, int]]) -> list[int]:
        """Set time-critical deadlines: [(piece, deadline_ms)]. Ref-counted across streams.

        Returns the pieces actually acquired (already-downloaded ones are skipped)."""
        acquired = []
        for p, deadline_ms in items:
            if self.have_piece(p):
                continue
            if self._deadline_refs[p] == 0:
                self.handle.set_piece_deadline(p, deadline_ms)
            self._deadline_refs[p] += 1
            acquired.append(p)
        return acquired

    def release_deadlines(self, pieces: list[int]) -> None:
        for p in pieces:
            n = self._deadline_refs.get(p, 0)
            if n <= 1:
                self._deadline_refs.pop(p, None)
                if not self.have_piece(p):
                    self.handle.reset_piece_deadline(p)
            else:
                self._deadline_refs[p] = n - 1

    def ensure_file_wanted(self, file_idx: int) -> None:
        """Like upstream stream-open: download the whole opened file in the background."""
        if self.handle.file_priority(file_idx) == 0:
            self.handle.file_priority(file_idx, 4)
        if file_idx in self.opened_files:
            self.opened_files.remove(file_idx)
        self.opened_files.append(file_idx)

    # -- lifecycle ---------------------------------------------------------------------------------
    async def save_resume_data(self, timeout: float = 5.0) -> bytes | None:
        if not self.handle.is_valid() or self._files is None:
            return None
        async with self._resume_lock:
            self._resume_future = self._loop.create_future()
            self.handle.save_resume_data(lt.save_resume_flags_t.save_info_dict)
            try:
                return await asyncio.wait_for(self._resume_future, timeout)
            except TimeoutError:
                return None
            finally:
                self._resume_future = None

    def release_memory(self) -> None:
        """Drop what only a playback needs (piece cache, built virtual files, subtitle state).

        Called when a torrent stops being watched: hundreds of seeding torrents must stay cheap."""
        self._piece_cache.clear()
        self._piece_cache_bytes = 0
        self.mux_cache.clear()
        self.subs = None
        self.selections.clear()
        self.logged_opened.clear()

    def remaining_bytes(self) -> int:
        """Bytes of the opened files not downloaded yet (what `completing` still has to fetch)."""
        if not self.opened_files or self._files is None or not self.handle.is_valid():
            return 0
        progress = self.handle.file_progress()
        return sum(
            max(0, self._files[i]["length"] - progress[i]) for i in self.opened_files if i < len(progress)
        )

    def fail_waiters(self) -> None:
        self.destroyed = True
        exc = EngineError("engine destroyed")
        for lst in self._waiters.have.values():
            for fut in lst:
                if not fut.done():
                    fut.set_exception(exc)
        for read_fut in self._waiters.read.values():
            if not read_fut.done():
                read_fut.set_exception(exc)
        self._waiters = _Waiters()
        self.metadata_ready.set()  # unblock metadata waiters; they re-check `destroyed`


def _routable_endpoint(a: Any) -> bool:
    try:
        host = str(a.local_endpoint[0])
    except Exception:  # noqa: BLE001
        return True
    return not (host.startswith(("fe80:", "127.")) or host == "::1")


def _alert_message(a: Any) -> str:
    for attr in ("error_message", "message"):
        fn = getattr(a, attr, None)
        if callable(fn):
            try:
                msg = fn()
                if msg:
                    return str(msg)
            except Exception:  # noqa: BLE001
                pass
    return str(a)
