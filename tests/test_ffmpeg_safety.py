"""ffmpeg on untrusted torrent files: only media demuxers, only HTTP to our server, bounded processes."""

from __future__ import annotations

import asyncio
import resource
import subprocess
from pathlib import Path

import pytest

from protoka.mux import convert, ffmpeg
from protoka.subs.model import SubtitleTrack
from tests.media import FFMPEG, make_audio, make_video, needs_ffmpeg

CONCAT = "ffconcat version 1.0\nfile other.mkv\n"  # concat would open other files next to it
HLS = "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttp://192.168.1.1/\n#EXT-X-ENDLIST\n"


def inputs(cmd: list[str]) -> list[list[str]]:
    """The options given to each `-i` (everything since the previous input)."""
    out, start = [], 0
    for i, arg in enumerate(cmd):
        if arg == "-i":
            out.append(cmd[start : i + 2])
            start = i + 2
    return out


def test_every_input_is_pinned(tmp_path):
    def track(fmt: str) -> SubtitleTrack:
        return SubtitleTrack(1, fmt, "", None, [], "[Script Info]\n", "utf-8", "eng", fmt)

    audio = [ffmpeg.ExtraAudio("http://127.0.0.1:1/ih/3?mux=0", "rus", "Dub")]
    plan = ffmpeg.FfmpegPlan(
        "http://127.0.0.1:1/ih/0?mux=0", [track("ssa"), track("srt")], [], {}, audio=audio
    )
    video, ass, sub, dub = inputs(ffmpeg.command("ffmpeg", plan, tmp_path))
    for opts, demuxers in ((video, ffmpeg.VIDEO_DEMUXERS), (dub, ffmpeg.AUDIO_DEMUXERS)):
        assert opts[opts.index("-protocol_whitelist") + 1] == "http,tcp"
        assert opts[opts.index("-format_whitelist") + 1] == demuxers
        assert "-rw_timeout" in opts
    for opts, fmt in ((ass, "ass"), (sub, "srt")):
        assert opts[-4:-2] == ["-f", fmt] and opts[opts.index("-protocol_whitelist") + 1] == "file"


@needs_ffmpeg
def test_whitelists_name_real_demuxers():
    out = subprocess.run(
        [FFMPEG, "-hide_banner", "-demuxers"], capture_output=True, text=True, check=True
    ).stdout
    rows = out.split("---", 1)[1].splitlines()
    known = {n for line in rows if line.strip() for n in line.split()[1].split(",")}
    for name in f"{ffmpeg.VIDEO_DEMUXERS},{ffmpeg.AUDIO_DEMUXERS}".split(","):
        assert name in known, name


@needs_ffmpeg
@pytest.mark.parametrize("name,body", [("video", CONCAT), ("playlist.m3u8", HLS)])
async def test_playlists_disguised_as_media_are_refused(tmp_path, http_url, caplog, name, body):
    (tmp_path / "other.mkv").write_bytes(make_video(tmp_path / "real.mkv", 1).read_bytes())
    url = http_url(tmp_path / name)
    (tmp_path / name).write_text(body)

    assert await ffmpeg.count_audio_streams(url) == 1  # "cannot tell", not the other file's streams

    out = convert.converted_path(tmp_path, 7)
    with pytest.raises(RuntimeError, match="not on whitelist"):
        await asyncio.wait_for(convert.ensure_mka(url, out), 30)
    assert not out.exists() and not out.with_name(out.name + ".part").exists()

    chunks = [c async for c in ffmpeg.stream(ffmpeg.FfmpegPlan(url, [], [], {}))]
    assert chunks == []
    assert "not on whitelist" in caplog.text


@needs_ffmpeg
async def test_local_files_cannot_be_read(tmp_path):
    with pytest.raises(RuntimeError, match="Protocol 'file' not on whitelist"):
        await convert.ensure_mka(f"file://{make_audio(tmp_path / 'a.flac', 1)}", tmp_path / "o.mka")


@needs_ffmpeg
async def test_allowed_containers_still_work(tmp_path, http_url):
    for name in ("v.mp4", "v.avi", "v.mkv"):
        url = http_url(make_video(tmp_path / name, 1, container=Path(name).suffix[1:]))
        assert await ffmpeg.count_audio_streams(url) == 1
        assert sum([len(c) async for c in ffmpeg.stream(ffmpeg.FfmpegPlan(url, [], [], {}))]) > 0
    for name in ("a.ac3", "a.flac", "a.mp3", "a.wav", "a.mka"):
        codec = {"ac3": "ac3", "mp3": "libmp3lame", "wav": "pcm_s16le"}.get(name[2:], "flac")
        src = make_audio(tmp_path / name, 1, codec=codec)
        assert (await convert.ensure_mka(http_url(src), tmp_path / f"{name}.out.mka")).stat().st_size > 0


async def test_limits_apply_to_the_child():
    proc = await asyncio.create_subprocess_exec("sleep", "5")
    try:
        ffmpeg.limit(proc)
        assert resource.prlimit(proc.pid, resource.RLIMIT_AS) == (ffmpeg.MAX_ADDRESS_SPACE,) * 2
        assert resource.prlimit(proc.pid, resource.RLIMIT_CPU) == (ffmpeg.MAX_CPU_SECONDS,) * 2
    finally:
        proc.kill()
        await proc.wait()
    ffmpeg.limit(proc)  # already gone: no error
