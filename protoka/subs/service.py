"""I/O layer of the subtitle pipeline: runs the pure steps and downloads only the files they need.

One `SubtitleService` per engine (torrent). Everything it computes is cached for the engine's life,
so repeated Range requests of a player never repeat work.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import TYPE_CHECKING

from protoka.stream import read_file
from protoka.subs import ass, convert, fonts, matching, names
from protoka.subs.archives import ArchiveUnsupported, extract_fonts
from protoka.subs.fallback import FallbackLibrary, Substitution
from protoka.subs.model import Assignment, FileName, FontFace, Match, SubtitleTrack

if TYPE_CHECKING:
    from protoka.engine import Engine

log = logging.getLogger(__name__)

MAX_SUBTITLE_BYTES = 20 * 1024 * 1024
MAX_FONT_SOURCE_BYTES = 100 * 1024 * 1024  # one font file or one font archive
MAX_FONTS_TOTAL_BYTES = 100 * 1024 * 1024  # attached to one video
DOWNLOAD_CONCURRENCY = 8


class SubtitleError(Exception):
    pass


class SubtitleService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._files: list[FileName] | None = None
        self._assignment: Assignment | None = None
        self._audio_assignment: Assignment | None = None
        # external audio per torrent file index: "mka" | "repacking" | "repacked" | "skipped: …"
        self.audio_status: dict[int, str] = {}
        self._tracks: dict[int, SubtitleTrack | SubtitleError] = {}
        self._faces: dict[str, list[FontFace]] = {}  # source id -> faces (an archive gives several)
        self._sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
        self.warnings: list[str] = []
        self.font_fallback: dict[int, list[Substitution]] = {}  # video idx -> replaced fonts, explained

    # -- pure steps (cached) -------------------------------------------------------------------
    @property
    def files(self) -> list[FileName]:
        if self._files is None:
            self._files = names.parse_files(self.engine.files or [])
        return self._files

    @property
    def assignment(self) -> Assignment:
        if self._assignment is None:
            self._assignment = matching.assign(self.files)
        return self._assignment

    def matches(self, video_idx: int) -> list[Match]:
        return self.assignment.by_video.get(video_idx, [])

    @property
    def audio_assignment(self) -> Assignment:
        """External audio files matched to videos — the same rules as subtitles (matching.assign)."""
        if self._audio_assignment is None:
            self._audio_assignment = matching.assign(self.files, names.Kind.AUDIO)
        return self._audio_assignment

    def audio_matches(self, video_idx: int) -> list[Match]:
        return self.audio_assignment.by_video.get(video_idx, [])

    def font_sources(self, video_idx: int) -> list[fonts.FontSource]:
        return fonts.locate(self.files, self.files[video_idx])

    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            log.warning("%s: %s", self.engine.info_hash, msg)
            self.warnings.append(msg)

    # -- subtitles -----------------------------------------------------------------------------
    async def track(self, match: Match) -> SubtitleTrack:
        """Download, decode and parse one matched subtitle file (cached, errors too)."""
        idx = match.sub.index
        cached = self._tracks.get(idx)
        if cached is None:
            try:
                async with self._sem:
                    data = await read_file(self.engine, idx, MAX_SUBTITLE_BYTES)
                cached = await asyncio.to_thread(build_track, match, data)  # charset detection is slow
            except (ValueError, ass.AssError) as e:
                cached = SubtitleError(f"{match.sub.path}: {e}")
                self.warn(str(cached))
            self._tracks[idx] = cached
        if isinstance(cached, SubtitleError):
            raise cached
        return cached

    async def tracks(self, video_idx: int) -> list[SubtitleTrack]:
        """All usable subtitle tracks for a video; broken files are skipped (and reported in warnings)."""
        results = await asyncio.gather(
            *(self.track(m) for m in self.matches(video_idx)), return_exceptions=True
        )
        out = []
        for r in results:
            if isinstance(r, SubtitleTrack):
                out.append(r)
            elif not isinstance(r, SubtitleError):
                raise r
        return out

    # -- fonts ---------------------------------------------------------------------------------
    async def _load_source(self, src: fonts.FontSource) -> list[FontFace]:
        if src.id in self._faces:
            return self._faces[src.id]
        faces: list[FontFace] = []
        try:
            async with self._sem:
                data = await read_file(self.engine, src.file_idx, MAX_FONT_SOURCE_BYTES)
            faces = await asyncio.to_thread(_faces_from, src, data)  # unzip + fontTools parsing
        except (ValueError, ArchiveUnsupported) as e:
            self.warn(f"fonts {src.path}: {e}")
        self._faces[src.id] = faces
        return faces

    async def fonts_for(
        self, video_idx: int, tracks: list[SubtitleTrack], fallback_fonts: FallbackLibrary | None = None
    ) -> tuple[list[SubtitleTrack], list[FontFace]]:
        """(tracks, fonts to attach): the fonts the ASS tracks reference, found in the torrent.

        A missing font is not fatal. With `fallback_fonts`, it is replaced by a bundled font with a similar
        letterform (subs/fallback.py): the tracks come back rewritten to name the substitute, and its subset
        is attached. Without it (or if nothing fits) the player picks a replacement itself."""
        needed = set().union(*(t.font_names for t in tracks)) if tracks else set()
        if not needed:
            return tracks, []
        sources = self.font_sources(video_idx)
        loaded = await asyncio.gather(*(self._load_source(s) for s in sources))
        all_faces = [f for faces in loaded for f in faces]
        picked, missing = fonts.pick(needed, all_faces)
        if missing:
            self.warn(f"fonts not found in torrent: {', '.join(sorted(missing))}")
        if missing and fallback_fonts is not None:
            tracks, extra = await self._fallback(video_idx, tracks, missing, fallback_fonts)
            picked = [*picked, *extra]
        total, out = 0, []
        for face in picked:
            total += len(face.data)
            if total > MAX_FONTS_TOTAL_BYTES:
                self.warn(f"font budget exceeded, skipped {face.filename}")
                continue
            out.append(face)
        return tracks, out

    async def _fallback(
        self, video_idx: int, tracks: list[SubtitleTrack], missing: set[str], library: FallbackLibrary
    ) -> tuple[list[SubtitleTrack], list[FontFace]]:
        scripts = [t.text for t in tracks if t.codec_private is not None]
        try:
            plan = await asyncio.to_thread(library.plan, missing, scripts)
        except Exception as e:  # noqa: BLE001 — a broken fallback font must not break playback
            self.warn(f"font fallback failed: {type(e).__name__}: {e}")
            return tracks, []
        self.font_fallback[video_idx] = plan.substitutions
        for name in plan.unresolved:
            self.warn(f"font {name}: no bundled fallback font is installed")
        if not plan.substitutes:
            return tracks, []
        out = []
        for t in tracks:
            if t.codec_private is not None and t.font_names & plan.substitutes.keys():
                t = with_text(t, ass.rename_fonts(t.text, plan.substitutes))
            out.append(t)
        return out, plan.faces

    async def face(self, face_id: str) -> FontFace | None:
        """A single font by id ("12" or "13/Fonts/a.ttf"), for the HTTP API."""
        source_id = face_id.split("/", 1)[0]
        if not source_id.isdigit():
            return None
        idx = int(source_id)
        if not (0 <= idx < len(self.files)):
            return None
        f = self.files[idx]
        src = fonts.FontSource(source_id, idx, f.path, f.kind is names.Kind.ARCHIVE, f.size)
        return next((x for x in await self._load_source(src) if x.id == face_id), None)


def _faces_from(src: fonts.FontSource, data: bytes) -> list[FontFace]:
    """Pure: a font file or a font archive → faces (runs in a worker thread)."""
    if src.is_archive:
        return [fonts.make_face(f"{src.id}/{m}", m, d) for m, d in extract_fonts(src.path, data)]
    return [fonts.make_face(src.id, src.path, data)]


def with_text(track: SubtitleTrack, text: str) -> SubtitleTrack:
    """Pure: the same ASS track with another script text (e.g. fonts renamed)."""
    codec_private, events, _ = ass.parse_ass(text)
    return dataclasses.replace(
        track, codec_private=codec_private, events=events, text=text, font_names=ass.font_names(text)
    )


def build_track(match: Match, data: bytes) -> SubtitleTrack:
    """Pure: subtitle bytes → SubtitleTrack (used by the service, easy to test on its own)."""
    text, encoding = convert.decode_text(data)
    lang, label = names.parse_lang(match.sub, match.video)
    ext = match.sub.ext.lstrip(".")
    if ext in ("ass", "ssa"):
        codec_private, events, is_ssa = ass.parse_ass(text)
        return SubtitleTrack(
            file_idx=match.sub.index,
            source_format=ext,
            codec_id="S_TEXT/SSA" if is_ssa else "S_TEXT/ASS",
            codec_private=codec_private,
            events=events,
            text=text,
            encoding=encoding,
            lang=lang,
            label=label,
            font_names=ass.font_names(text),
        )
    events = convert.parse_vtt(text) if ext == "vtt" else convert.parse_srt(text)
    if not events:
        raise ValueError("no subtitle cues found")
    return SubtitleTrack(
        file_idx=match.sub.index,
        source_format=ext,
        codec_id="S_TEXT/UTF8",
        codec_private=None,
        events=events,
        text=text,
        encoding=encoding,
        lang=lang,
        label=label,
    )
