"""Decide how a video is served and build that once per (torrent, file).

    open(engine, idx) ->
        None          nothing to embed, muxing disabled, or something failed  → original file, as before
        MuxedVirtual  MKV with Cues → virtual MKV (seekable, Range)
        MuxedFfmpeg   anything else ffmpeg can read → streamed remux (no Range)

What gets embedded: sidecar subtitles (+ their fonts) and sidecar audio, found by `subs` (same matching
rules for both). Audio per file:
    .mka that fits        its clusters are copied lazily from the torrent (mux/audio.py, mux/patch.py)
    raw / unfit .mka      repacked to MKA by ffmpeg (mux/convert.py); the build waits up to
                          `audio_wait` seconds, then goes on without it (the repack continues for next time)

The decision and the built layout are cached on the engine: a player sends many Range requests and
all of them must see exactly the same virtual file. Failures are cached too. Any error falls back to
the original file.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from istok import logfmt
from istok.mux import audio as audio_mod
from istok.mux import convert, ffmpeg
from istok.mux.audio import AudioError, SidecarAudio
from istok.mux.layout import VIDEO, LayoutError, build
from istok.mux.mkv import MkvError, MkvIndex, read_index
from istok.mux.virtual import VirtualFile
from istok.stream import FileStream, read_range
from istok.subs.fallback import FallbackLibrary
from istok.subs.model import FontFace, Match, SubtitleTrack
from istok.subs.names import MKA_EXTS, parse_lang
from istok.subs.service import SubtitleService

if TYPE_CHECKING:
    from istok.engine import Engine

log = logging.getLogger(__name__)

BUILD_TIMEOUT = 20.0
AUDIO_EXTRA_TIME = 15.0  # on top of the audio wait: index reading, subtitles, layout
MKV_EXTS = (".mkv", ".mk3d", ".mka", ".webm")


@dataclass
class MuxedVirtual:
    file: VirtualFile
    summary: dict[str, Any]


@dataclass
class MuxedFfmpeg:
    plan: ffmpeg.FfmpegPlan
    summary: dict[str, Any]


Muxed = MuxedVirtual | MuxedFfmpeg


@dataclass
class _Entry:
    """One build per (engine, file). `task` is owned by the service, not by a request: a client that
    disconnects mid-build cancels only its own wait, never the build other requests are waiting for."""

    task: asyncio.Task[None] | None = None
    result: Muxed | None = None
    reason: str = ""
    done: asyncio.Event = field(default_factory=asyncio.Event)


def subtitle_service(engine: Engine) -> SubtitleService:
    if engine.subs is None:
        engine.subs = SubtitleService(engine)
    return engine.subs


def _torrent_reader(engine: Engine, file_idx: int) -> Callable[[int, int], Any]:
    size = engine.files[file_idx]["length"]  # type: ignore[index]

    async def read(offset: int, n: int) -> bytes:
        end = min(size, offset + n) - 1
        return await read_range(engine, file_idx, offset, end) if end >= offset else b""

    return read


def _local_reader(path: Path) -> Callable[[int, int], Any]:
    async def read(offset: int, n: int) -> bytes:
        return b"".join([c async for c in convert.read_local(path, offset, offset + n - 1)])

    return read


class MuxService:
    def __init__(
        self,
        *,
        enabled: Callable[[], bool],
        source_url: Callable[[str, int], str],
        timeout: float = BUILD_TIMEOUT,
        audio_wait: Callable[[], float] = lambda: 90.0,
        preferred_audio_lang: Callable[[], str | None] = lambda: None,
        fallback_fonts: Callable[[], FallbackLibrary | None] = lambda: None,
    ):
        self._enabled = enabled  # read live from settings
        self._source_url = source_url  # (info_hash, idx) -> URL of the unmuxed file, for ffmpeg
        self.timeout = timeout
        self._audio_wait = audio_wait
        self._preferred_audio_lang = preferred_audio_lang
        self._fallback_fonts = fallback_fonts  # bundled substitutes for missing fonts, or None (off)

    def status(self, engine: Engine, idx: int) -> dict[str, Any] | None:
        entry = self._cache(engine).get(idx)
        if entry is None or not entry.done.is_set():
            return None
        if entry.result is None:
            return {"kind": "original", "reason": entry.reason}
        return entry.result.summary

    def _cache(self, engine: Engine) -> dict[int, _Entry]:
        return engine.mux_cache

    async def open(self, engine: Engine, idx: int) -> Muxed | None:
        if not self._enabled():
            return None
        subs = subtitle_service(engine)
        if not subs.matches(idx) and not subs.audio_matches(idx):
            return None
        cache = self._cache(engine)
        entry = cache.get(idx)
        if entry is None:
            entry = cache[idx] = _Entry()
            entry.task = asyncio.create_task(self._run_build(entry, engine, idx, subs))
        await asyncio.shield(entry.done.wait())
        return entry.result

    def _timeout_for(self, subs: SubtitleService, idx: int) -> float:
        if subs.audio_matches(idx):
            return max(self.timeout, self._audio_wait() + AUDIO_EXTRA_TIME)
        return self.timeout

    async def _run_build(self, entry: _Entry, engine: Engine, idx: int, subs: SubtitleService) -> None:
        # everything inside try: whatever happens, `entry.done` is set and waiting requests are released
        timeout = self.timeout
        started = time.monotonic()
        tag = logfmt.tag(engine, idx)
        failed = False
        try:
            timeout = self._timeout_for(subs, idx)
            entry.result = await asyncio.wait_for(
                self._build(engine, idx, subs, time.monotonic() + timeout), timeout
            )
            if entry.result is None:
                entry.reason = "no usable subtitles or audio"
        except TimeoutError:
            entry.reason, failed = f"building took longer than {timeout:g}s", True
        except Exception as e:  # noqa: BLE001 — never break playback
            log.exception("%s mux: build failed", tag)
            entry.reason, failed = f"{type(e).__name__}: {e}", True
        finally:
            took = logfmt.secs(time.monotonic() - started)
            if entry.result is None:
                level = logging.WARNING if failed else logging.INFO
                log.log(level, "%s mux: done in %s → original file: %s", tag, took, entry.reason)
                subs.warnings.append(f"mux: {entry.reason}")
            else:
                log.info("%s mux: ready in %s → %s", tag, took, self._describe(engine, idx, entry.result))
            entry.done.set()

    @staticmethod
    def _describe(engine: Engine, idx: int, result: Muxed) -> str:
        if isinstance(result, MuxedVirtual):
            source = engine.files[idx]["length"]  # type: ignore[index]
            grown = result.file.size - source
            return f"virtual MKV {logfmt.size(result.file.size)} (+{logfmt.size(grown)} of tracks and fonts)"
        return "ffmpeg remux (this container cannot be rebuilt in place; no seeking)"

    # -- the build --------------------------------------------------------------------------------
    async def _build(self, engine: Engine, idx: int, subs: SubtitleService, deadline: float) -> Muxed | None:
        tag = logfmt.tag(engine, idx)
        sub_matches, audio_matches = subs.matches(idx), subs.audio_matches(idx)
        n_fonts = len(subs.font_sources(idx))
        log.info(
            "%s mux: building — found %d subtitle(s), %d audio track(s), %d font source(s)",
            tag, len(sub_matches), len(audio_matches), n_fonts,
        )  # fmt: skip
        tracks = await subs.tracks(idx)
        by_file = {t.file_idx: t for t in tracks}
        for m in sub_matches:
            t = by_file.get(m.sub.index)
            if t is None:
                problem = next((w for w in subs.warnings if w.startswith(m.sub.path)), "unreadable")
                log.info(
                    "%s mux:   - subtitle %s: %s", tag, m.sub.path, problem.removeprefix(f"{m.sub.path}: ")
                )
                continue
            log.info(
                '%s mux:   + subtitle %s "%s" ← %s (%s, %d events, %s)',
                tag, t.lang or "und", t.label, m.sub.path, t.source_format.upper(), len(t.events), t.encoding,
            )  # fmt: skip
        needed = set().union(*(t.font_names for t in tracks)) if tracks else set()
        tracks, fonts = await subs.fonts_for(idx, tracks, self._fallback_fonts())
        if needed:
            fallback = subs.font_fallback.get(idx, [])
            for s in fallback:
                log.info("%s mux:   ~ font %s", tag, s.describe())
            replaced = {s.missing for s in fallback}
            covered = set().union(*(f.families for f in fonts)) if fonts else set()
            missing = sorted(needed - covered - replaced)
            names_ = ", ".join(f.filename for f in fonts) or "none"
            tail = f" (missing, left to the player: {', '.join(missing)})" if missing else ""
            log.info("%s mux:   + fonts %s%s", tag, names_, tail)
        if not tracks and not audio_matches:
            return None
        name = (engine.files or [])[idx]["name"].lower()
        if name.endswith(MKV_EXTS):
            try:
                read = _torrent_reader(engine, idx)
                index = await read_index(read, engine.files[idx]["length"])  # type: ignore[index]
                sidecars = await self._sidecars(engine, index, audio_matches, subs, deadline)
                if not tracks and not sidecars:
                    return None
                return self._virtual(engine, idx, index, tracks, fonts, sidecars)
            except (MkvError, LayoutError) as e:
                log.info("%s mux: cannot rebuild this MKV in place (%s), using ffmpeg", tag, e)
        return await self._ffmpeg_plan(engine, idx, tracks, fonts, audio_matches)

    async def _sidecars(
        self,
        engine: Engine,
        video: MkvIndex,
        matches: list[Match],
        subs: SubtitleService,
        deadline: float,
    ) -> list[SidecarAudio]:
        """External audio files that can go into the virtual MKV, in match order (best first)."""
        preferred = self._preferred_audio_lang()
        tag = logfmt.tag(engine, matches[0].video.index) if matches else logfmt.tag(engine)
        found: list[SidecarAudio] = []
        how: dict[int, str] = {}  # file idx -> how the track got in, for the log
        repacks: list[tuple[Match, str | None, str, asyncio.Task[Path]]] = []
        for m in matches:
            fi = m.sub.index
            lang, label = parse_lang(m.sub, m.video)
            if m.sub.ext in MKA_EXTS:
                try:
                    index = await read_index(_torrent_reader(engine, fi), m.sub.size)
                    for w in audio_mod.check_compatible(video, index):
                        subs.warn(f"audio {m.sub.path}: {w}")
                    found.append(SidecarAudio(f"file:{fi}", fi, index, lang, label))
                    how[fi] = "copied from .mka"
                    subs.audio_status[fi] = "mka"
                    engine.ensure_file_wanted(fi)  # keep downloading it in the background
                    continue
                except (MkvError, AudioError) as e:
                    log.info(
                        "%s mux:   ~ audio %s cannot be copied as is (%s): repacking", tag, m.sub.path, e
                    )
            if ffmpeg.ffmpeg_binary() is None:
                subs.audio_status[fi] = "skipped: ffmpeg is not installed"
                log.info("%s mux:   - audio %s: ffmpeg is not installed", tag, m.sub.path)
                continue
            out = convert.converted_path(engine.save_path, fi)
            task = convert.ensure_mka(self._source_url(engine.info_hash, fi), out, tag=logfmt.tag(engine, fi))
            subs.audio_status[fi] = "repacking"
            repacks.append((m, lang, label, task))

        wait_until = min(deadline - AUDIO_EXTRA_TIME / 2, time.monotonic() + self._audio_wait())
        for m, lang, label, task in repacks:
            fi = m.sub.index
            try:
                wait_from = time.monotonic()
                path = await asyncio.wait_for(asyncio.shield(task), max(0.0, wait_until - time.monotonic()))
                index = await read_index(_local_reader(path), path.stat().st_size)
                for w in audio_mod.check_compatible(video, index):
                    subs.warn(f"audio {m.sub.path}: {w}")
                found.append(SidecarAudio(f"local:{path}", fi, index, lang, label))
                how[fi] = f"repacked, build waited {logfmt.secs(time.monotonic() - wait_from)} for it"
                subs.audio_status[fi] = "repacked"
            except TimeoutError:
                subs.audio_status[fi] = "repacking (not ready in time; next playback will have it)"
                log.info("%s mux:   - audio %s: still repacking, not in this playback", tag, m.sub.path)
                subs.warn(
                    f"audio {m.sub.path}: repack not ready in {self._audio_wait():g}s, playing without it"
                )
            except (MkvError, AudioError, RuntimeError, OSError) as e:
                subs.audio_status[fi] = f"skipped: {e}"
                subs.warn(f"audio {m.sub.path}: {e}")
                log.info("%s mux:   - audio %s: skipped: %s", tag, m.sub.path, e)

        found.sort(key=lambda s: [m.sub.index for m in matches].index(s.file_idx))
        if preferred:
            for i, s in enumerate(found):
                if s.lang == preferred:
                    found[i] = dataclasses.replace(s, default=True)
                    break
        paths = {m.sub.index: m.sub.path for m in matches}
        for s in found:
            default = f", default track (PREFERRED_AUDIO_LANG={preferred})" if s.default else ""
            log.info(
                '%s mux:   + audio %s "%s" ← %s (%s%s)',
                tag, s.lang or "und", s.label, paths[s.file_idx], how.get(s.file_idx, ""), default,
            )  # fmt: skip
        return found

    def _virtual(
        self,
        engine: Engine,
        idx: int,
        index: MkvIndex,
        tracks: list[SubtitleTrack],
        fonts: list[FontFace],
        sidecars: list[SidecarAudio],
    ) -> MuxedVirtual:
        layout = build(index, tracks, fonts, sidecars)

        def read(key: str, start: int, end: int) -> AsyncIterator[bytes]:
            if key == VIDEO:
                return aiter(FileStream(engine, idx, start, end))
            kind, _, ref = key.partition(":")
            if kind == "file":
                return aiter(FileStream(engine, int(ref), start, end))
            if kind == "local":
                return convert.read_local(Path(ref), start, end)
            raise ValueError(f"unknown source {key!r}")

        return MuxedVirtual(VirtualFile(layout, read), layout.summary)

    async def _ffmpeg_plan(
        self,
        engine: Engine,
        idx: int,
        tracks: list[SubtitleTrack],
        fonts: list[FontFace],
        audio_matches: list[Match],
    ) -> MuxedFfmpeg:
        if ffmpeg.ffmpeg_binary() is None:
            raise FileNotFoundError("ffmpeg is not installed: cannot embed tracks into this container")
        preferred = self._preferred_audio_lang()
        audio: list[ffmpeg.ExtraAudio] = []
        for m in audio_matches:
            lang, label = parse_lang(m.sub, m.video)
            audio.append(ffmpeg.ExtraAudio(self._source_url(engine.info_hash, m.sub.index), lang, label))
        if preferred:
            for i, a in enumerate(audio):
                if a.lang == preferred:
                    audio[i] = dataclasses.replace(a, default=True)
                    break
        tag = logfmt.tag(engine, idx)
        for a, m in zip(audio, audio_matches, strict=True):
            default = f", default track (PREFERRED_AUDIO_LANG={preferred})" if a.default else ""
            log.info(
                '%s mux:   + audio %s "%s" ← %s (read by ffmpeg%s)',
                tag,
                a.lang or "und",
                a.label,
                m.sub.path,
                default,
            )
        source = self._source_url(engine.info_hash, idx)
        plan = ffmpeg.FfmpegPlan(source, tracks, fonts, {}, audio=audio)
        plan.summary = {
            "kind": "ffmpeg-remux",
            "seekable": False,
            "tracks": [
                {"codec": t.codec_id, "lang": t.lang, "label": t.label, "fileIdx": t.file_idx} for t in tracks
            ],
            "audio": [
                {"lang": a.lang, "label": a.label, "default": a.default, "fileIdx": m.sub.index}
                for a, m in zip(audio, audio_matches, strict=True)
            ],
            "attachments": [f.filename for f in fonts],
        }
        return MuxedFfmpeg(plan, plan.summary)
