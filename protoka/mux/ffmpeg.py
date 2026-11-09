"""Remux a non-MKV source (AVI, MP4, MKV without Cues) into Matroska with ffmpeg, streamed.

    ffmpeg -i <server URL of the original file, ?mux=0> -i sub0 -i sub1 …
           -map 0:v -map 0:a? -map 1:0 -map 2:0 … -c copy -attach font … -f matroska pipe:1

ffmpeg reads the source through our own HTTP endpoint, so it can seek inside it (MP4 `moov` at the end,
AVI index) while pieces are fetched from the torrent on demand. The output is a pipe: it can be played
from the start but not seeked (no Content-Length / Range) — that is the price for not re-implementing
AVI/MP4 muxing.

Every input is untrusted (any torrent file), so ffmpeg never picks a demuxer or protocol freely
(analysis/28-ffmpeg-whitelist.md): `url_input` allows only media demuxers (no concat, HLS, image2…) and
only HTTP to our own server, our subtitle files are opened with a fixed demuxer, and `limit` caps a
runaway process. A refused input is an ordinary ffmpeg failure, handled by the existing fallbacks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import resource
import shutil
import tempfile
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from protoka.subs import convert
from protoka.subs.model import FontFace, SubtitleTrack

log = logging.getLogger(__name__)

CHUNK = 256 * 1024
_running: dict[str, asyncio.subprocess.Process] = {}  # source URL -> the ffmpeg remuxing it


# demuxers allowed on torrent files; ffmpeg matches any of a demuxer's names (`mov` = mov,mp4,m4a,…)
VIDEO_DEMUXERS = "matroska,mov,avi,asf,mpeg,mpegts,flv,ogg,rm"  # every subs.names.VIDEO_EXTS container
AUDIO_DEMUXERS = (
    "matroska,mov,ac3,eac3,dts,dtshd,truehd,mlp,aac,flac,mp3,wav,w64,ogg"  # .mka + RAW_AUDIO_EXTS
)
STALL_SECONDS = 300  # no bytes from the torrent for this long (no peers) → ffmpeg gives up
MAX_ADDRESS_SPACE = 2 * 1024**3  # `-c copy` needs far less; only a runaway process hits these
MAX_CPU_SECONDS = 3600


def url_input(url: str, demuxers: str) -> list[str]:
    """`-i url` for a file read through our own HTTP endpoint: these demuxers only, HTTP only (nested
    opens inherit the protocol whitelist, so no `file:` and no other hosts' schemes), and a stall timeout."""
    return [
        "-protocol_whitelist", "http,tcp",
        "-rw_timeout", str(STALL_SECONDS * 1_000_000),
        "-format_whitelist", demuxers,
        "-i", url,
    ]  # fmt: skip


def limit(proc: asyncio.subprocess.Process) -> None:
    """Cap memory and CPU of a child just started (not via preexec_fn: unsafe with our worker threads)."""
    for res, value in ((resource.RLIMIT_AS, MAX_ADDRESS_SPACE), (resource.RLIMIT_CPU, MAX_CPU_SECONDS)):
        with contextlib.suppress(OSError, AttributeError):  # exited already / no prlimit off Linux
            resource.prlimit(proc.pid, res, (value, value))


def ffmpeg_binary() -> str | None:
    return os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")


@dataclass(frozen=True)
class ExtraAudio:
    """A sidecar audio file, read by ffmpeg through our HTTP endpoint (its first audio stream)."""

    url: str
    lang: str | None
    label: str
    default: bool = False


@dataclass
class FfmpegPlan:
    source_url: str
    tracks: Sequence[SubtitleTrack]
    fonts: Sequence[FontFace]
    summary: dict
    audio: Sequence[ExtraAudio] = field(default_factory=list)
    source_audio_streams: int = 1  # audio streams in the source video (filled in by `stream`)


def _subtitle_file(track: SubtitleTrack, workdir: Path, i: int) -> Path:
    if track.source_format in ("ass", "ssa"):
        path = workdir / f"sub{i}.{track.source_format}"
        path.write_text(track.text, "utf-8")
    else:  # SRT and VTT both go in as SRT (S_TEXT/UTF8)
        path = workdir / f"sub{i}.srt"
        path.write_text(convert.to_srt(track.events), "utf-8")
    return path


def command(binary: str, plan: FfmpegPlan, workdir: Path) -> list[str]:
    cmd = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-fflags",
        "+genpts",
        *url_input(plan.source_url, VIDEO_DEMUXERS),
    ]
    sub_files = [_subtitle_file(t, workdir, i) for i, t in enumerate(plan.tracks)]
    for p in sub_files:  # written by us: the demuxer is fixed by the extension we chose
        fmt = "srt" if p.suffix == ".srt" else "ass"  # the `ass` demuxer reads SSA too
        cmd += ["-protocol_whitelist", "file", "-f", fmt, "-i", str(p)]
    for a in plan.audio:
        cmd += url_input(a.url, AUDIO_DEMUXERS)
    cmd += ["-map", "0:v", "-map", "0:a?"]
    first_audio_input = 1 + len(sub_files)
    for k in range(len(plan.audio)):
        cmd += ["-map", f"{first_audio_input + k}:a:0"]
    for i in range(len(sub_files)):
        cmd += ["-map", f"{i + 1}:0"]
    cmd += ["-c", "copy"]
    # output audio streams: the source's own first, then the sidecars
    base = plan.source_audio_streams
    for k, a in enumerate(plan.audio):
        j = base + k
        cmd += [f"-metadata:s:a:{j}", f"language={a.lang or 'und'}", f"-metadata:s:a:{j}", f"title={a.label}"]
    if any(a.default for a in plan.audio):
        for j in range(base):
            cmd += [f"-disposition:a:{j}", "0"]
        for k, a in enumerate(plan.audio):
            cmd += [f"-disposition:a:{base + k}", "default" if a.default else "0"]
    for i, t in enumerate(plan.tracks):
        cmd += [f"-metadata:s:s:{i}", f"language={t.lang or 'und'}", f"-metadata:s:s:{i}", f"title={t.label}"]
    for j, face in enumerate(plan.fonts):
        path = workdir / f"font{j}_{face.filename}"
        path.write_bytes(face.data)
        cmd += ["-attach", str(path), f"-metadata:s:t:{j}", f"mimetype={face.mime}"]
    cmd += ["-f", "matroska", "pipe:1"]
    return cmd


async def count_audio_streams(url: str) -> int:
    """Audio streams in a source (ffprobe reads only its header); 1 if it cannot tell."""
    probe = os.environ.get("FFPROBE_BIN") or shutil.which("ffprobe")
    if probe is None:
        return 1
    proc = await asyncio.create_subprocess_exec(
        probe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0",
        *url_input(url, VIDEO_DEMUXERS),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    limit(proc)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 30)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 1
    return len([line for line in out.decode().splitlines() if line.strip()]) if proc.returncode == 0 else 1


async def _tail(reader: asyncio.StreamReader, keep: int = 2000) -> str:
    tail = b""
    while chunk := await reader.read(4096):
        tail = (tail + chunk)[-keep:]
    return tail.decode(errors="replace")


async def stream(plan: FfmpegPlan) -> AsyncIterator[bytes]:
    """Run ffmpeg and yield its output; the process is killed when the consumer stops (client left)."""
    binary = ffmpeg_binary()
    if binary is None:
        raise FileNotFoundError("ffmpeg not found (set FFMPEG_BIN)")
    if plan.audio:
        plan.source_audio_streams = await count_audio_streams(plan.source_url)
    with tempfile.TemporaryDirectory(prefix="stremio-remux-") as tmp:
        cmd = command(binary, plan, Path(tmp))
        log.debug("remux: %s", " ".join(cmd))
        # one remux per file: a player reads only its newest connection (probe, then play, then a
        # re-open after a seek), so an older ffmpeg reading the same source from the start is waste
        previous = _running.get(plan.source_url)
        if previous is not None and previous.returncode is None:
            log.info("remux: replacing the previous ffmpeg for %s", plan.source_url)
            with contextlib.suppress(ProcessLookupError):
                previous.kill()
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        limit(proc)
        _running[plan.source_url] = proc
        if proc.stdout is None or proc.stderr is None:  # cannot happen with PIPE; not an assert (python -O)
            raise RuntimeError("ffmpeg started without pipes")
        # read stderr concurrently: a full stderr pipe would stall ffmpeg
        stderr_task = asyncio.create_task(_tail(proc.stderr))
        try:
            while chunk := await proc.stdout.read(CHUNK):
                yield chunk
            await proc.wait()
            if proc.returncode:
                log.warning("ffmpeg exited with %s: %s", proc.returncode, await stderr_task)
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
            stderr_task.cancel()
            if _running.get(plan.source_url) is proc:
                del _running[plan.source_url]
