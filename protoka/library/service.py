"""LibraryService: keeps every cached torrent seeding, fetches the next episode, evicts when full.

One tick (every 10 s, and right away when something happens — a stream opens or closes, a torrent is
added, settings change):

    snapshot every torrent (TorrentView)  →  policy.decide()  →  apply to libtorrent
                                          →  eviction.choose_victims()  →  delete victims
                                          →  save library.json

Applying a decision is idempotent: a handle is only touched when its target settings change.
    streaming    not auto-managed, downloads, all connections, upload not limited; with `prefetch` the next
                 episode's files (video, subtitles, audio, font sources) get file priority 1
    seeding      auto-managed + upload_mode: uploads what is here, never downloads; libtorrent's queue picks
                 the active seeds (seed_rank); BG_MAX_CONNECTIONS
    done         paused
While something is being watched, the other torrents share STREAM_BG_UPLOAD_LIMIT. Once the next episode is
downloaded, `on_prefetched` (set by the HTTP app) repacks its raw external audio to disk ahead of time.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import libtorrent as lt

from protoka import logfmt
from protoka.library import eviction, next_episode, policy
from protoka.library.model import Decision, Limits, State, TorrentView
from protoka.library.store import LibraryStore
from protoka.subs import names
from protoka.subs.model import Kind

if TYPE_CHECKING:
    from protoka.engine import Engine
    from protoka.session import TorrentSession

log = logging.getLogger(__name__)

TICK = 10.0
STARTUP_BATCH = 10  # restored torrents handed to libtorrent per step
STARTUP_STEP = 2.0
DISK_SCAN_EVERY = 30.0
CACHE_LOG_EVERY = 600.0  # "cache: 12.30 GB of 50.00 GB …" (also right after start and after an eviction)
_BG_FLAGS = lt.torrent_flags.auto_managed | lt.torrent_flags.upload_mode
_NO = Decision("", State.SEEDING)


@dataclass(frozen=True)
class Prefetch:
    video_idx: int  # the next episode
    name: str
    files: tuple[int, ...]  # the video and its sidecars (next_episode.episode_files), downloaded whole


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            st = p.lstat()
        except OSError:
            continue
        if p.is_file():
            total += st.st_blocks * 512 if hasattr(st, "st_blocks") else st.st_size
    return total


class LibraryService:
    def __init__(self, session: TorrentSession):
        self.session = session
        self.store = LibraryStore(session.settings.app_path)
        self.decisions: dict[str, Decision] = {}
        self.prefetch: dict[str, Prefetch | None] = {}  # ih -> target (for applying and the report)
        self._prefetch_for: dict[str, int] = {}  # ih -> the watched video the target was computed for
        self.startup_pending: list[str] = []
        self.last_eviction: eviction.Plan | None = None
        self.evicted: list[dict[str, Any]] = []  # recent evictions, for seeding.json
        self._sizes: dict[str, int] = {}
        self._sizes_at = 0.0
        self._cache_logged_at: float | None = None
        self._short_logged = 0  # last reported shortfall over CACHE_SIZE (0 = fits): logged on change only
        self._wakeup = asyncio.Event()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._startup_task: asyncio.Task | None = None
        self._global_upload: int | None = None
        self._line_free, self._line_why = True, ""  # policy.line_free() of the last tick
        # the next episode of `ih` (video index) is downloaded: the HTTP app prepares it (audio repack)
        self.on_prefetched: Callable[[Engine, int], None] | None = None
        # what was last logged, so every event is written once, when it changes
        self._prefetch_log: dict[str, tuple[str, float]] = {}  # ih -> (phase, since) phase: active|waiting

    # -- configuration ----------------------------------------------------------------------------
    def limits(self) -> Limits:
        s = self.session.settings
        return Limits(
            seed_ratio=float(s["seedRatioLimit"] or 0),
            seed_time=float(s["seedTimeLimit"] or 0),
            stream_grace=float(s["streamGrace"]),
        )

    # -- loop -------------------------------------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        if self.startup_pending:
            self._startup_task = asyncio.create_task(self._release_startup())

    async def stop(self) -> None:
        for t in (self._task, self._startup_task):
            if t is not None:
                t.cancel()
        self.store.save()

    def poke(self) -> None:
        """Run a tick as soon as possible (a stream opened/closed, a torrent was added…)."""
        self._wakeup.set()

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wakeup.wait(), TICK)
            except TimeoutError:
                pass
            self._wakeup.clear()
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — the loop must survive anything
                log.exception("library tick failed")

    async def _release_startup(self) -> None:
        """Gradual start: hand restored torrents to libtorrent a batch at a time (no announce storm)."""
        log.info(
            "library: restored %d torrents from the cache; starting them %d at a time",
            len(self.startup_pending), STARTUP_BATCH,
        )  # fmt: skip
        while self.startup_pending:
            del self.startup_pending[:STARTUP_BATCH]
            self.poke()
            await asyncio.sleep(STARTUP_STEP)
        log.info("library: all restored torrents are handed to libtorrent and seed again")

    # -- one tick ---------------------------------------------------------------------------------
    async def tick(self, now: float | None = None) -> list[Decision]:
        async with self._lock:
            now = time.time() if now is None else now
            engines = [e for e in self.session.engines.values() if not e.destroyed]
            await self._refresh_sizes(engines)
            views = [self._view(e) for e in engines]
            decisions = policy.decide(views, self.limits(), now)
            self.decisions = {d.ih: d for d in decisions}
            self._line_free, self._line_why = policy.line_free(views, self.limits(), now)
            streaming = any(d.state is State.STREAMING for d in decisions)
            bg_active = [d for d in decisions if d.state not in (State.STREAMING, State.DONE)]
            share = self._background_upload_share(streaming, len(bg_active))
            self._apply_global_upload()
            by_ih = {e.info_hash: e for e in engines}
            for d in decisions:
                e = by_ih[d.ih]
                if d.ih in self.startup_pending:
                    continue  # restored, not handed to libtorrent yet
                self._apply(e, d, share, now)
                self._log_prefetch(e, d, now)
            if self.session.settings.caching_disabled:
                for d in decisions:
                    if d.state is not State.STREAMING:
                        await self.session.purge(d.ih, reason="caching disabled")
            else:
                await self._evict(views, {d.ih: d.state for d in decisions})
            self.store.save()
            return decisions

    def _view(self, e: Engine) -> TorrentView:
        st = e.handle.status() if e.handle.is_valid() else None
        item = self.store.get(e.info_hash)
        target = self._prefetch_target(e)
        pending = target is not None and self._prefetch_pending(e, target)
        return TorrentView(
            ih=e.info_hash,
            name=getattr(e, "name", None) or item.get("name") or e.info_hash,
            open_streams=e.open_streams,
            internal_reads=e.internal_reads,
            last_activity=e.last_activity,
            remaining_bytes=e.remaining_bytes(),
            prefetch_pending=pending,
            size_on_disk=self._sizes.get(e.info_hash, 0),
            # all_time_* lag behind the live counters for a while: take the larger one
            uploaded=max(st.all_time_upload, st.total_payload_upload) if st else 0,
            downloaded=max(st.all_time_download, st.total_payload_download) if st else 0,
            seeding_since=item.get("seeding_since"),
            last_watched=e.last_watched,
            seeds=max(0, st.num_complete) if st else 0,
            leechers=max(0, st.num_incomplete) if st else 0,
            has_metadata=e.files is not None,
        )

    async def _refresh_sizes(self, engines: list[Engine]) -> None:
        if time.monotonic() - self._sizes_at < DISK_SCAN_EVERY and all(
            e.info_hash in self._sizes for e in engines
        ):
            return
        paths = {e.info_hash: e.save_path for e in engines}
        self._sizes = await asyncio.to_thread(lambda: {ih: _dir_size(p) for ih, p in paths.items()})
        self._sizes_at = time.monotonic()

    # -- applying decisions -----------------------------------------------------------------------
    def _background_upload_share(self, streaming: bool, n_background: int) -> int:
        limit = int(self.session.settings["streamBgUploadLimit"] or 0)
        if not streaming or limit <= 0 or n_background == 0:
            return -1
        return max(1024, limit // n_background)

    def _apply_global_upload(self) -> None:
        limit = int(self.session.settings["seedUploadLimit"] or 0)
        if limit != self._global_upload:
            self.session.ses.apply_settings({"upload_rate_limit": limit})
            self._global_upload = limit

    def _apply(self, e: Engine, d: Decision, share: int, now: float) -> None:
        s = self.session.settings
        previous = e.state
        e.state = d.state.value
        if d.state is State.STREAMING:
            key: tuple = (d.state, int(s["btMaxConnections"]), -1)
        else:
            key = (d.state, int(s["bgMaxConnections"]), share)
        if d.prefetch:
            key += (self.prefetch.get(e.info_hash),)
        if previous == State.STREAMING.value and d.state is not State.STREAMING:
            e.release_memory()
        if previous != d.state.value:
            log.info("%s library: %s → %s (%s)", logfmt.tag(e), previous, d.state.value, "; ".join(d.reasons))
        if d.state is State.SEEDING and self.store.get(e.info_hash).get("seeding_since") is None:
            self.store.update(e.info_hash, seeding_since=now)
        self.store.update(e.info_hash, name=getattr(e, "name", None), last_watched=e.last_watched)
        if e.applied == key or not e.handle.is_valid():
            return
        h = e.handle
        state, max_conn, upload = key[0], key[1], key[2]
        h.set_max_connections(max_conn)
        h.set_upload_limit(upload)
        if state is State.DONE:
            h.unset_flags(lt.torrent_flags.auto_managed)
            h.pause()
        elif state is State.SEEDING:
            h.set_flags(_BG_FLAGS)  # libtorrent's queue decides when it actually runs
        else:  # streaming: downloads
            h.unset_flags(lt.torrent_flags.auto_managed | lt.torrent_flags.upload_mode)
            h.resume()
        if d.prefetch:
            target = self.prefetch.get(e.info_hash)
            if target is not None:
                for fi in target.files:  # whole files: the data goes to the real files, not the part file
                    if h.file_priority(fi) == 0:
                        h.file_priority(fi, 1)  # below the episode being watched (4)
        e.applied = key

    # -- prefetch ---------------------------------------------------------------------------------
    def _prefetch_target(self, e: Engine) -> Prefetch | None:
        s = self.session.settings
        if not s["prefetchNext"] or e.files is None:
            return None
        files = names.parse_files(e.files)
        videos = [i for i in e.opened_files if i < len(files) and files[i].kind is Kind.VIDEO]
        if not videos:
            return None
        current = videos[-1]
        if self._prefetch_for.get(e.info_hash) == current:
            return self.prefetch.get(e.info_hash)
        nxt = next_episode.next_video(files, current)
        target = None
        if nxt is not None:
            wanted = next_episode.episode_files(files, nxt.index)
            target = Prefetch(nxt.index, nxt.path, tuple(wanted))
            kinds = [files[i].kind for i in wanted[1:]]
            parts = [
                f"{n} {what}"
                for n, what in (
                    (kinds.count(Kind.SUBTITLE), "subtitle(s)"),
                    (kinds.count(Kind.AUDIO), "audio track(s)"),
                    (len(kinds) - kinds.count(Kind.SUBTITLE) - kinds.count(Kind.AUDIO), "font source(s)"),
                )
                if n
            ]
            total = sum(e.files[i]["length"] for i in wanted)
            extras = "".join(f" + {p}" for p in parts)
            log.info(
                '%s prefetch: next episode is "%s" — video%s, %s',
                logfmt.tag(e, current), nxt.stem + nxt.ext, extras, logfmt.size(total),
            )  # fmt: skip
        elif sum(f.kind is Kind.VIDEO for f in files) > 1:  # a single movie: nothing to say
            log.info('%s prefetch: no next episode after "%s"', logfmt.tag(e, current), files[current].stem)
        self._prefetch_for[e.info_hash] = current
        self.prefetch[e.info_hash] = target
        return target

    # -- event log ----------------------------------------------------------------------------------
    def _log_prefetch(self, e: Engine, d: Decision, now: float) -> None:
        """prefetch: started / waiting — why / done in Ns — each once, when the phase changes."""
        target = self.prefetch.get(e.info_hash)
        if target is None:
            self._prefetch_log.pop(e.info_hash, None)
            return
        pending = self._prefetch_pending(e, target)
        phase, since = self._prefetch_log.get(e.info_hash, ("", now))
        tag = logfmt.tag(e, target.video_idx)
        if not pending:
            if phase:
                log.info("%s prefetch: done in %s", tag, logfmt.secs(now - since))
                self._prefetch_log.pop(e.info_hash, None)
                if self.on_prefetched is not None:
                    self.on_prefetched(e, target.video_idx)
            return
        if d.prefetch and phase != "active":
            log.info("%s prefetch: started", tag)
            self._prefetch_log[e.info_hash] = ("active", since if phase else now)
        elif not d.prefetch and phase != "waiting":
            why = self._line_why if not self._line_free else f"{logfmt.tag(e)} is not being watched"
            log.info("%s prefetch: waiting — %s", tag, why)
            self._prefetch_log[e.info_hash] = ("waiting", since if phase else now)

    def _prefetch_pending(self, e: Engine, target: Prefetch) -> bool:
        h = e.handle
        if not h.is_valid():
            return False
        return self._prefetch_remaining(e, target) > 0

    def _prefetch_remaining(self, e: Engine, target: Prefetch) -> int:
        progress = e.handle.file_progress()
        return sum(max(0, e.files[fi]["length"] - progress[fi]) for fi in target.files)  # type: ignore[index]

    # -- eviction ---------------------------------------------------------------------------------
    async def _evict(self, views: list[TorrentView], states: dict[str, State]) -> None:
        s = self.session.settings
        cache_dir = s.cache_dir()
        reserve = sum(v.remaining_bytes for v in views if states.get(v.ih) is State.STREAMING)
        reserve += sum(
            self._prefetch_remaining(self.session.engines[ih], p)
            for ih, p in self.prefetch.items()
            if p is not None and self.decisions.get(ih, _NO).prefetch and ih in self.session.engines
        )
        free = shutil.disk_usage(cache_dir).free
        plan = eviction.choose_victims(
            views,
            states,
            budget=s.cache_size,
            reserve=reserve,
            free_disk=free,
            min_free=int(s["minFreeDisk"]),
            now=time.time(),
            keep_recent=float(s["cacheKeepRecent"]),
        )
        self.last_eviction = plan
        self._log_cache_size(plan, states, free)
        for ih, reason in plan.victims:
            victim = self.session.engines.get(ih)
            log.warning("%s cache: evicting — %s", logfmt.tag(victim) if victim else f"[{ih[:8]}]", reason)
            await self.session.purge(ih, reason=reason)
            self.store.forget(ih)
            self._sizes.pop(ih, None)
            self.prefetch.pop(ih, None)
            self._prefetch_for.pop(ih, None)
            self.evicted = ([{"infoHash": ih, "reason": reason, "at": time.time()}] + self.evicted)[:20]
        self._log_shortfall(plan)

    def _log_shortfall(self, plan: eviction.Plan) -> None:
        """Really over CACHE_SIZE on disk with nothing (more) allowed to go: once when it starts, once when it
        is over. Not the forecast with the reserve: that one moves every tick."""
        short = plan.over_used
        if short > 0 and not self._short_logged:
            log.warning(
                "cache: %s over CACHE_SIZE; kept, as only what is being or was recently watched "
                "(CACHE_KEEP_RECENT) remains",
                logfmt.size(short),
            )  # fmt: skip
        elif short == 0 and self._short_logged:
            log.info("cache: within CACHE_SIZE again")
        self._short_logged = short

    def _log_cache_size(self, plan: eviction.Plan, states: dict[str, State], free: int) -> None:
        """Periodic summary of the cache: how full it is, what it holds, what is reserved, disk headroom."""
        now = time.monotonic()
        if (
            not plan.victims
            and self._cache_logged_at is not None
            and now - self._cache_logged_at < CACHE_LOG_EVERY
        ):
            return
        self._cache_logged_at = now
        s = self.session.settings
        if s.cache_size == float("inf"):
            fill = f"{logfmt.size(plan.used)} (no CACHE_SIZE limit)"
        else:
            pct = 100 * plan.used / s.cache_size if s.cache_size else 0
            fill = f"{logfmt.size(plan.used)} of {logfmt.size(s.cache_size)} ({pct:.0f}%)"
        counts: dict[str, int] = {}
        for st in states.values():
            counts[st.value] = counts.get(st.value, 0) + 1
        by_state = ", ".join(f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1])) or "empty"
        log.info(
            "cache: %s in %d torrent(s) [%s]; reserved %s for downloads in progress; "
            "%s free on disk (min %s)",
            fill, len(states), by_state, logfmt.size(plan.reserve), logfmt.size(free),
            logfmt.size(int(s["minFreeDisk"])),
        )  # fmt: skip

    # -- report -----------------------------------------------------------------------------------
    def report(self) -> dict[str, Any]:
        s = self.session.settings
        plan = self.last_eviction
        limits = self.limits()
        engines = [e for e in self.session.engines.values() if not e.destroyed]
        views = {v.ih: v for v in (self._view(e) for e in engines)}
        free, why = policy.line_free(list(views.values()), limits, time.time())
        torrents: list[dict[str, Any]] = []
        for e in engines:
            v = views[e.info_hash]
            d = self.decisions.get(e.info_hash)
            st = e.handle.status() if e.handle.is_valid() else None
            target = self.prefetch.get(e.info_hash)
            torrents.append(
                {
                    "infoHash": e.info_hash,
                    "name": v.name,
                    "state": "starting"
                    if e.info_hash in self.startup_pending
                    else (d.state.value if d else e.state),
                    "reasons": list(d.reasons) if d else [],
                    "libtorrentPaused": bool(st and st.flags & lt.torrent_flags.paused),
                    "sizeOnDisk": v.size_on_disk,
                    "uploaded": v.uploaded,
                    "downloaded": v.downloaded,
                    "ratio": round(v.ratio, 3),
                    "uploadSpeed": st.upload_payload_rate if st else 0,
                    "downloadSpeed": st.download_payload_rate if st else 0,
                    "peers": st.num_peers if st else 0,
                    "swarmSeeds": v.seeds,
                    "swarmLeechers": v.leechers,
                    "remainingBytes": v.remaining_bytes,
                    "lastWatched": v.last_watched,
                    "seedingSince": v.seeding_since,
                    "prefetch": {"next": target.name, "pending": v.prefetch_pending} if target else None,
                }
            )
        torrents.sort(key=lambda t: -float(t["lastWatched"] or 0))
        return {
            "cache": {
                "budget": None if s.cache_size == float("inf") else int(s.cache_size),
                "used": plan.used if plan else sum(self._sizes.values()),
                "reserve": plan.reserve if plan else 0,
                "free": shutil.disk_usage(s.cache_dir()).free,
                "minFree": int(s["minFreeDisk"]),
                "keepRecent": s["cacheKeepRecent"],
                "overBy": plan.over_used if plan else 0,
            },
            "nextEpisode": {"allowed": free, "why": why},
            "limits": {
                "seedUploadLimit": s["seedUploadLimit"],
                "streamBgUploadLimit": s["streamBgUploadLimit"],
                "seedMaxActive": s["seedMaxActive"],
                "seedRatioLimit": s["seedRatioLimit"],
                "seedTimeLimit": s["seedTimeLimit"],
                "bgMaxConnections": s["bgMaxConnections"],
                "streamGrace": s["streamGrace"],
                "prefetchNext": s["prefetchNext"],
            },
            "torrents": torrents,
            "recentlyEvicted": self.evicted,
        }
