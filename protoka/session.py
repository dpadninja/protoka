"""Single libtorrent session for the whole process + engine registry and lifecycle.

Peer discovery fixes vs upstream (analysis/02-peer-discovery.md):
  P1 listen on a real port (TCP+uTP, IPv4+IPv6) with UPnP/NAT-PMP; P2 one real peer id;
  P3 one DHT node, state persisted across runs; P4 IPv6; P5 PEX/LSD/uTP; P6 higher limits;
  P7 no pausing of peer search; P8 trackers follow their interval; P9 sources merged; P11 errors in stats.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from collections.abc import Awaitable, Iterable
from pathlib import Path
from typing import Any

import libtorrent as lt

from protoka.config import Settings
from protoka.engine import Engine, EngineError, SourceStats
from protoka.library.service import LibraryService
from protoka.trackers import DHT_BOOTSTRAP_NODES, merge_trackers

log = logging.getLogger(__name__)

STATE_FILE = "bt-session.state"
RESUME_FILE = "resume.dat"
TORRENT_FILE = "metadata.torrent"

ALERT_MASK = (
    lt.alert_category.error
    | lt.alert_category.status
    | lt.alert_category.storage
    | lt.alert_category.tracker
    | lt.alert_category.dht
    | lt.alert_category.piece_progress
    | lt.alert_category.port_mapping
    | lt.alert_category.stats  # session_stats_alert → live DHT node count
)


# DHT state: first save once the routing table has filled up after bootstrap, then regularly, so a
# crash (SIGKILL, OOM) loses at most one interval; plus a save on graceful shutdown.
STATE_FIRST_SAVE = 120.0
STATE_SAVE_INTERVAL = 5 * 60.0
SESSION_STATS_INTERVAL = 5.0
RESUME_SAVE_INTERVAL = 5 * 60.0  # resume data of changed torrents: a crash must not force a recheck


def count_saved_nodes(buf: bytes) -> int:
    """DHT nodes in a saved session state (bencoded `{"dht state": {"nodes": [...], "nodes6": [...]}}`).

    Read from the bencoded bytes: the Python binding cannot convert `session_params.dht_state.nodes`
    (std::vector<udp::endpoint> has no registered converter)."""
    try:
        state: Any = lt.bdecode(buf)  # structure is checked by hand below
        dht = state.get(b"dht state") if isinstance(state, dict) else None
        if not isinstance(dht, dict):
            return 0
        return sum(len(dht.get(key) or []) for key in (b"nodes", b"nodes6"))
    except Exception:  # noqa: BLE001 — corrupt file
        return 0


class TorrentSession:
    def __init__(
        self, settings: Settings, *, overrides: dict[str, Any] | None = None, default_trackers: bool = True
    ):
        self.settings = settings
        self.overrides = overrides or {}
        self.default_trackers = default_trackers
        self.engines: dict[str, Engine] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._alert_r: int | None = None
        self._alert_w: int | None = None
        self._tasks: list[asyncio.Task] = []
        self._background: set[asyncio.Future] = set()  # strong refs: unreferenced tasks may be GC'd
        # DHT persistence report (shown in /stats.json?sys=1)
        self.dht_nodes = 0  # live, from session_stats_alert
        self.dht_nodes_loaded = 0  # read from the state file at start
        self.state_saved_at: float | None = None
        self._warned_empty_state = False
        self.ses = self._create_session()
        self.library = LibraryService(self)

    # -- session setup ----------------------------------------------------------------------------
    def settings_pack(self) -> dict[str, Any]:
        s = self.settings
        port = s["btListenPort"]
        pack: dict[str, Any] = {
            "user_agent": "libtorrent/" + lt.__version__,
            "listen_interfaces": f"0.0.0.0:{port},[::]:{port}",
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
            "enable_incoming_utp": True,
            "enable_outgoing_utp": True,
            "enable_incoming_tcp": True,
            "enable_outgoing_tcp": True,
            "dht_bootstrap_nodes": ",".join(DHT_BOOTSTRAP_NODES),
            "alert_mask": ALERT_MASK,
            "alert_queue_size": 10000,
            # no queue for what we manage ourselves (streaming / background); seeds: see below
            "active_downloads": -1,
            "active_limit": -1,
            "active_checking": 4,
            # keep connections even when everything wanted is downloaded (warm swarm for seeks)
            "close_redundant_connections": False,
            # seeding (analysis/15-seeding.md): serve peers that just started or almost finished first —
            # the most useful upload for a swarm; libtorrent's queue rotates the active seeds
            "seed_choking_algorithm": int(lt.seed_choking_algorithm_t.anti_leech),
            "active_seeds": int(s["seedMaxActive"]) or -1,
            "dont_count_slow_torrents": True,
            "upload_rate_limit": int(s["seedUploadLimit"] or 0),
            "announce_to_all_trackers": True,
            "announce_to_all_tiers": True,
            "connections_limit": max(500, s["btMaxConnections"] * 3),
            "connection_speed": 100,
            "torrent_connect_boost": 50,
            "handshake_timeout": max(5, s["btHandshakeTimeout"] // 1000),
            "peer_connect_timeout": 7,
            "max_peerlist_size": 4000,
            "smooth_connects": False,
            "strict_end_game_mode": False,
            "request_timeout": max(10, s["btRequestTimeout"] // 1000 * 3),
        }
        pack.update(self.overrides)
        return pack

    @property
    def state_path(self) -> Path:
        return self.settings.app_path / STATE_FILE

    def _create_session(self) -> lt.session:
        params = lt.session_params()
        try:
            buf = self.state_path.read_bytes()
            params = lt.read_session_params(buf, lt.save_state_flags_t.save_dht_state)
            self.dht_nodes_loaded = count_saved_nodes(buf)
            log.info("Loaded DHT state: %d nodes from %s", self.dht_nodes_loaded, self.state_path)
        except FileNotFoundError:
            log.info("No saved DHT state, bootstrapping from routers")
        except Exception as e:  # noqa: BLE001
            log.warning("Cannot load session state: %s", e)
        params.settings = self.settings_pack()  # type: ignore[assignment]  # the binding takes dicts
        return lt.session(params)

    def _apply_torrent_limits(self, h: lt.torrent_handle) -> None:
        h.set_max_connections(self.settings["btMaxConnections"])
        # btDownloadSpeedSoft/HardLimit are kept only for UI compatibility: upstream paused the swarm
        # (and peer search, P7) above the soft limit. A libtorrent rate limit also delays every
        # request until the next quota tick (~0.5 s), which hurts startup latency, so it is opt-in.
        limit = self.settings["btDownloadLimit"]
        h.set_download_limit(int(limit) if limit else -1)

    @property
    def listen_port(self) -> int:
        return self.ses.listen_port()

    def save_state(self) -> bool:
        """Persist the DHT routing table atomically. Returns whether the file was written.

        An empty routing table (offline, still bootstrapping) is never written over a saved one."""
        try:
            st = self.ses.session_state(flags=lt.save_state_flags_t.save_dht_state)
            data = bytes(lt.write_session_params_buf(st, lt.save_state_flags_t.save_dht_state))
            nodes = count_saved_nodes(data)
            if nodes == 0 and self.state_path.exists():
                if not self._warned_empty_state:
                    log.warning("DHT has no nodes yet; keeping the saved state %s", self.state_path)
                    self._warned_empty_state = True
                return False
            self.settings.app_path.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())  # survive a power loss, not only a crash
            os.replace(tmp, self.state_path)
            self.state_saved_at = time.time()
            self._warned_empty_state = False
            log.info("Saved DHT state: %d nodes", nodes)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("Cannot save session state: %s", e)
            return False

    def dht_report(self) -> dict[str, Any]:
        saved = self.state_saved_at
        return {
            "nodes": self.dht_nodes,
            "nodesLoadedAtStart": self.dht_nodes_loaded,
            "stateSavedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(saved)) if saved else None,
            "stateFile": str(self.state_path),
        }

    # -- alert pump ------------------------------------------------------------------------------
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        r, w = os.pipe()
        os.set_blocking(r, False)
        os.set_blocking(w, False)
        self._alert_r, self._alert_w = r, w
        self.ses.set_alert_fd(w)
        loop.add_reader(r, self._on_alert_fd)
        self._tasks.append(asyncio.create_task(self._periodic()))
        await self.restore_cached()
        self.library.start()
        log.info("BitTorrent session listening on port %s", self.listen_port)

    async def _periodic(self) -> None:
        now = time.monotonic()
        next_save = now + STATE_FIRST_SAVE
        next_stats = now
        next_resume = now + RESUME_SAVE_INTERVAL
        while True:
            await asyncio.sleep(min(1.0, STATE_FIRST_SAVE, STATE_SAVE_INTERVAL))
            self.pump_alerts()  # safety net if an fd notification was coalesced
            now = time.monotonic()
            if now >= next_stats:
                next_stats = now + SESSION_STATS_INTERVAL
                self.ses.post_session_stats()
            if now >= next_save:
                next_save = now + STATE_SAVE_INTERVAL
                self.save_state()
            if now >= next_resume:
                next_resume = now + RESUME_SAVE_INTERVAL
                self.background(self.save_all_resume())

    def _on_alert_fd(self) -> None:
        with contextlib.suppress(BlockingIOError, InterruptedError):
            while os.read(self._alert_r, 4096):  # type: ignore[arg-type]
                pass
        self.pump_alerts()

    def pump_alerts(self) -> None:
        for a in self.ses.pop_alerts():
            try:
                self._dispatch(a)
            except Exception:  # noqa: BLE001
                log.exception("alert handler failed for %s", type(a).__name__)

    def _dispatch(self, a: Any) -> None:
        name = type(a).__name__
        if name in ("listen_failed_alert", "portmap_error_alert"):
            log.warning("%s: %s", name, a.message())
        elif name in ("listen_succeeded_alert", "portmap_alert"):
            log.info("%s", a.message())
        elif name == "session_stats_alert":
            self.dht_nodes = int(a.values.get("dht.dht_nodes", 0))
            return
        handle = getattr(a, "handle", None)
        if handle is None:
            return
        try:
            ih = str(handle.info_hash())
        except Exception:  # noqa: BLE001
            return
        e = self.engines.get(ih)
        if e is not None:
            e.on_alert(a)

    async def close(self) -> None:
        """Save everything and stop. Torrents stay in the cache and seed again on the next start."""
        for t in self._tasks:
            t.cancel()
        await self.library.stop()
        await self.save_all_resume()
        for e in list(self.engines.values()):
            e.fail_waiters()
        self.save_state()
        if self._alert_r is not None:
            asyncio.get_running_loop().remove_reader(self._alert_r)
            os.close(self._alert_r)
            os.close(self._alert_w)  # type: ignore[arg-type]
            self._alert_r = self._alert_w = None
        self.ses.pause()

    # -- engines ----------------------------------------------------------------------------------
    def get(self, info_hash: str) -> Engine | None:
        return self.engines.get(info_hash.lower())

    def info_hashes(self) -> list[str]:
        return list(self.engines)

    def cache_path(self, info_hash: str) -> Path:
        return self.settings.cache_dir() / info_hash

    async def create(
        self,
        info_hash: str,
        *,
        sources: Iterable[object] | None = None,
        torrent: bytes | None = None,
        options: dict[str, Any] | None = None,
        touch: bool = True,
    ) -> Engine:
        """Get or create the engine for `info_hash` (lowercase hex). Does not wait for metadata.

        `touch=False`: an internal request (our ffmpeg) — it does not count as someone watching."""
        ih = info_hash.lower()
        lock = self._locks.setdefault(ih, asyncio.Lock())
        async with lock:
            e = self.engines.get(ih)
            if e is not None and e.destroyed:
                self.engines.pop(ih, None)  # a purge is in flight; re-create
                e = None
            if e is not None and not e.destroyed:
                if sources:
                    self._add_trackers(e, merge_trackers(sources, include_defaults=False))
                if touch:
                    self.touch(e)
                return e
            e = await self._add(ih, sources, torrent, options or {})
            self.engines[ih] = e
            self.touch(e)  # also wakes the library: eviction makes room if needed
            log.info("Engine created for %s", ih)
            return e

    async def _add(
        self,
        ih: str,
        sources: Iterable[object] | None,
        torrent: bytes | None,
        options: dict[str, Any],
        *,
        paused: bool = False,
    ) -> Engine:
        save_path = self.cache_path(ih)
        save_path.mkdir(parents=True, exist_ok=True)
        atp = None
        resume = save_path / RESUME_FILE
        if resume.exists():
            try:
                atp = lt.read_resume_data(resume.read_bytes())
            except Exception as e:  # noqa: BLE001
                log.warning("Bad resume data for %s: %s", ih, e)
                atp = None
        announce: list[str] = []
        if torrent is not None:
            loaded = lt.load_torrent_buffer(torrent)
            announce = list(loaded.trackers)
            if atp is None:
                atp = loaded
            else:
                atp.ti = loaded.ti
        elif atp is None and (save_path / TORRENT_FILE).exists():
            try:
                atp = lt.load_torrent_file(str(save_path / TORRENT_FILE))
            except Exception as e:  # noqa: BLE001
                log.warning("Bad cached metadata for %s: %s", ih, e)
                atp = None
        if atp is None:
            atp = lt.parse_magnet_uri(f"magnet:?xt=urn:btih:{ih}")
        trackers = merge_trackers(
            sources, announce, list(atp.trackers), include_defaults=self.default_trackers
        )
        atp.trackers = trackers
        atp.tracker_tiers = list(range(len(trackers)))
        atp.save_path = str(save_path)
        flags = atp.flags | lt.torrent_flags.default_dont_download
        flags &= ~lt.torrent_flags.auto_managed
        flags &= ~lt.torrent_flags.upload_mode
        flags = flags | lt.torrent_flags.paused if paused else flags & ~lt.torrent_flags.paused
        atp.flags = flags
        atp.max_connections = self.settings["btMaxConnections"]
        handle = self.ses.add_torrent(atp)
        self._apply_torrent_limits(handle)
        opts = self._engine_options(ih, trackers, save_path, options)
        return Engine(self, ih, handle, save_path, trackers, opts)

    def _engine_options(
        self, ih: str, trackers: list[str], save_path: Path, options: dict[str, Any]
    ) -> dict[str, Any]:
        s = self.settings
        opts = {k: v for k, v in options.items() if k not in ("torrent", "blob")}
        opts.update(
            {
                "peerSearch": {
                    "min": 40,
                    "max": 150,
                    "sources": [f"tracker:{t}" for t in trackers] + [f"dht:{ih}"],
                },
                "dht": True,
                "tracker": True,
                "connections": s["btMaxConnections"],
                "handshakeTimeout": s["btHandshakeTimeout"],
                "timeout": s["btRequestTimeout"],
                "path": str(save_path),
                "virtual": True,
                # not used by this engine (see P7), but stremio-core requires them in `opts`
                "growler": {"flood": 0, "pulse": s["btDownloadLimit"] or None},
                "swarmCap": {"maxSpeed": None, "minPeers": s["btMinPeersForStable"]},
                "listenPort": self.listen_port,
            }
        )
        return opts

    def _add_trackers(self, e: Engine, trackers: list[str]) -> None:
        known = {t["url"] for t in e.handle.trackers()}  # announce entries are dicts
        for i, url in enumerate(trackers):
            if url not in known:
                e.handle.add_tracker({"url": url, "tier": len(known) + i})
                e.sources.setdefault(url, SourceStats(f"tracker:{url}"))

    def on_metadata(self, e: Engine) -> None:
        """Persist metadata so a re-open after restart does not need peers to fetch it again."""
        ti = e.handle.torrent_file()
        if ti is None:
            return
        try:
            data = b"d4:info" + bytes(ti.info_section()) + b"e"
            (e.save_path / TORRENT_FILE).write_bytes(data)
        except OSError as ex:
            log.warning("Cannot save metadata for %s: %s", e.info_hash, ex)

    # -- stream accounting / lifecycle ------------------------------------------------------------
    def stream_opened(self, e: Engine) -> None:
        e.open_streams += 1
        e.last_watched = time.time()
        self.touch(e)

    def stream_closed(self, e: Engine) -> None:
        e.open_streams = max(0, e.open_streams - 1)
        self.touch(e)

    def touch(self, e: Engine) -> None:
        """Mark activity (create, stats, stream): the torrent counts as "being watched" for STREAM_GRACE.

        Torrents are never unloaded on inactivity any more: the library turns them into seeds."""
        e.last_activity = time.time()
        if e.inactive_handle is not None:
            e.inactive_handle.cancel()
            e.inactive_handle = None
        if not e.destroyed:
            loop = asyncio.get_running_loop()
            grace = float(self.settings["streamGrace"])
            e.inactive_handle = loop.call_later(grace + 0.5, self.library.poke)  # re-evaluate after grace
        self.library.poke()

    def background(self, aw: Awaitable[Any]) -> None:
        """Run fire-and-forget work while keeping a reference until it finishes."""
        fut = asyncio.ensure_future(aw)
        self._background.add(fut)
        fut.add_done_callback(self._background.discard)

    async def remove(self, info_hash: str, *, purge: bool = False) -> bool:
        """`/:ih/remove`: stop "being watched" right away (the torrent keeps seeding from the cache).

        `purge=True` deletes it from the session and from disk."""
        ih = info_hash.lower()
        e = self.engines.get(ih)
        if e is None or e.destroyed:
            return False
        if purge:
            return await self.purge(ih, reason="requested")
        e.open_streams = 0
        e.last_activity = 0.0  # grace is over
        self.library.poke()
        return True

    async def purge(self, info_hash: str, *, reason: str = "") -> bool:
        """Remove a torrent from the session and delete its cache directory."""
        ih = info_hash.lower()
        e = self.engines.get(ih)
        if e is None or e.destroyed:
            path = self.cache_path(ih)
            if path.exists():  # an orphan directory (no engine)
                shutil.rmtree(path, ignore_errors=True)
                return True
            return False
        e.destroyed = True
        if e.inactive_handle is not None:
            e.inactive_handle.cancel()
        self.engines.pop(ih, None)
        e.fail_waiters()
        if e.handle.is_valid():
            self.ses.remove_torrent(e.handle, lt.session.delete_files)
        shutil.rmtree(e.save_path, ignore_errors=True)
        log.info("Engine %s deleted (%s)", ih, reason or "purge")
        return True

    async def remove_all(self, *, purge: bool = False) -> None:
        await asyncio.gather(*(self.remove(ih, purge=purge) for ih in list(self.engines)))

    async def save_all_resume(self) -> None:
        """Write resume data of every torrent that changed (periodic, and on shutdown)."""

        async def one(e: Engine) -> None:
            if not e.handle.is_valid() or not e.handle.need_save_resume_data():
                return
            data = await e.save_resume_data()
            if data:
                with contextlib.suppress(OSError):
                    (e.save_path / RESUME_FILE).write_bytes(data)

        await asyncio.gather(*(one(e) for e in list(self.engines.values()) if not e.destroyed))

    async def restore_cached(self) -> int:
        """Load every torrent found in the cache (resume.dat, else metadata.torrent) so it seeds again.

        They are added paused; the library hands them to libtorrent gradually."""
        root = self.settings.cache_dir()
        restored = 0
        for d in sorted(root.iterdir()) if root.exists() else []:
            ih = d.name.lower()
            if not d.is_dir() or len(ih) != 40 or ih in self.engines:
                continue
            if not ((d / RESUME_FILE).exists() or (d / TORRENT_FILE).exists()):
                continue  # an orphan: eviction deletes it when space is needed
            try:
                e = await self._add(ih, None, None, {}, paused=True)
            except Exception as ex:  # noqa: BLE001 — one broken torrent must not stop the rest
                log.warning("cannot restore %s from the cache: %s", ih, ex)
                continue
            e.last_activity = 0.0
            e.last_watched = float(self.library.store.get(ih).get("last_watched") or 0)
            self.engines[ih] = e
            self.library.startup_pending.append(ih)
            restored += 1
        if restored:
            log.info("Restored %d torrents from the cache; they will start seeding gradually", restored)
        return restored


__all__ = ["TorrentSession", "EngineError"]
