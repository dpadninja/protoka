"""External audio in the virtual MKV: sidecar MKA copied lazily with renumbered tracks, raw streams repacked."""

from __future__ import annotations

import asyncio
import random
from dataclasses import replace
from pathlib import Path

import pytest

from protoka.mux import audio as audio_mod
from protoka.mux import convert
from protoka.mux.audio import AudioError, SidecarAudio
from protoka.mux.layout import build
from protoka.mux.mkv import read_index
from protoka.mux.patch import PatchError, patch_clusters
from protoka.mux.virtual import VirtualFile
from tests.media import (
    FFMPEG,
    FFPROBE,
    ffmpeg_errors,
    make_audio,
    make_video,
    needs_ffmpeg,
    packets_md5,
    probe,
)

pytestmark = needs_ffmpeg


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    if not (FFMPEG and FFPROBE):
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("audio")
    return {
        "video": make_video(d / "video.mkv", 30),
        "mka": make_audio(d / "side.mka", 30),
        "ac3": make_audio(d / "side.ac3", 30, codec="ac3", freq=550),
    }


async def index_of(path: Path):
    data = path.read_bytes()

    async def read(offset: int, n: int) -> bytes:
        return data[offset : offset + n]

    return await read_index(read, len(data))


def reader(files: dict[str, Path]):
    blobs = {key: p.read_bytes() for key, p in files.items()}

    async def read(key: str, start: int, end: int):
        yield blobs[key][start : end + 1]

    return read


async def virtual(
    media, tmp_path: Path, *, default: bool = True, lang: str = "rus"
) -> tuple[VirtualFile, bytes, Path]:
    video = await index_of(media["video"])
    side = SidecarAudio("file:1", 1, await index_of(media["mka"]), lang, "RUS Sound [Grp]", default=default)
    vf = VirtualFile(build(video, [], [], [side]), reader({"video": media["video"], "file:1": media["mka"]}))
    data = b"".join([c async for c in vf.iter_range(0, vf.size - 1)])
    out = tmp_path / "virt.mkv"
    out.write_bytes(data)
    return vf, data, out


async def test_sidecar_is_compatible_and_cut_into_spans(media):
    video, side = await index_of(media["video"]), await index_of(media["mka"])
    assert audio_mod.check_compatible(video, side) == []
    spans = audio_mod.spans(side)
    assert len(spans) >= 25 and spans[0].src == side.first_cluster
    assert sum(s.length for s in spans) == side.clusters_end - side.first_cluster  # contiguous, nothing lost


async def test_virtual_mkv_gets_the_external_track_untouched(media, tmp_path):
    vf, _, out = await virtual(media, tmp_path)
    streams = [s for s in probe(str(out))["streams"] if s["codec_type"] == "audio"]
    assert len(streams) == 2
    ext = streams[1]
    assert (ext["codec_name"], ext["tags"]["language"], ext["tags"]["title"]) == (
        "flac",
        "rus",
        "RUS Sound [Grp]",
    )
    assert ext["disposition"]["default"] == 1 and streams[0]["disposition"]["default"] == 0
    # audio frames byte-identical to the sidecar, video untouched
    assert packets_md5(str(out), "0:a:1") == packets_md5(str(media["mka"]), "0:a:0")
    assert packets_md5(str(out), "0:v") == packets_md5(str(media["video"]), "0:v")
    assert packets_md5(str(out), "0:a:0") == packets_md5(str(media["video"]), "0:a:0")
    assert vf.layout.summary["audio"][0]["default"] is True


def crc_errors(data: bytes, start: int, end: int) -> list[int]:
    """Clusters in data[start:end] whose CRC-32 is wrong — an independent check (ffmpeg's crccheck
    does not verify clusters): EBML CRC-32 is the IEEE CRC of the parent payload after the CRC element,
    stored little-endian."""
    import struct
    import zlib

    from protoka.mux import ebml
    from protoka.mux.ebml import ID

    bad, pos = [], start
    while pos < end:
        h = ebml.read_header(data, pos)
        if h.id == ID.CLUSTER:
            first = ebml.read_header(data, h.data_offset)
            if first.id == ID.CRC32:
                stored = struct.unpack("<I", data[first.data_offset : first.end])[0]
                if zlib.crc32(data[first.end : h.end]) & 0xFFFFFFFF != stored:
                    bad.append(pos)
        pos = h.end
    return bad


async def test_crc_is_recomputed_after_patching(media, tmp_path):
    vf, data, _ = await virtual(media, tmp_path)
    side = await index_of(media["mka"])
    original = media["mka"].read_bytes()
    assert crc_errors(original, side.first_cluster, side.clusters_end) == []  # the checker agrees with ffmpeg
    from protoka.mux.layout import Patched

    patched = [
        (off, c) for off, c in zip(vf.layout.offsets, vf.layout.chunks, strict=True) if isinstance(c, Patched)
    ]
    assert patched
    for off, chunk in patched:
        assert crc_errors(data, off, off + chunk.length) == [], off
    # without recomputing, the CRC would be wrong: flip a payload byte and the checker must notice
    off, chunk = patched[2]
    broken = bytearray(data)
    broken[off + chunk.length - 5] ^= 0xFF
    assert crc_errors(bytes(broken), off, off + chunk.length) != []


async def test_seek_into_the_external_track(media, tmp_path):
    import json
    import subprocess

    _, _, out = await virtual(media, tmp_path)
    proc = subprocess.run(
        [FFPROBE, "-v", "error", "-read_intervals", "20%+1", "-select_streams", "a:1",
         "-show_entries", "packet=pts_time", "-of", "json", str(out)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    times = [float(p["pts_time"]) for p in json.loads(proc.stdout)["packets"]]
    assert times and 19.0 <= times[0] <= 21.0
    assert ffmpeg_errors(str(out), "-ss", "20") == ""


async def test_no_default_change_without_preferred_language(media, tmp_path):
    _, _, out = await virtual(media, tmp_path, default=False)
    streams = [s for s in probe(str(out))["streams"] if s["codec_type"] == "audio"]
    source = [s for s in probe(str(media["video"]))["streams"] if s["codec_type"] == "audio"]
    assert streams[0]["disposition"]["default"] == source[0]["disposition"]["default"]  # left as it was
    assert streams[1]["disposition"]["default"] == 0


async def test_sidecar_language_wins_unless_undefined(media, tmp_path):
    """ffmpeg/mkvmerge write Language "und" when nobody set one: then the folder guess is used."""
    _, _, out = await virtual(media, tmp_path, lang="rus")
    assert [s for s in probe(str(out))["streams"] if s["codec_type"] == "audio"][1]["tags"][
        "language"
    ] == "rus"
    tagged = tmp_path / "tagged.mka"
    import subprocess

    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(media["mka"]), "-c", "copy", "-metadata:s:a:0",
                    "language=jpn", "-cluster_time_limit", "1000", str(tagged)], check=True)  # fmt: skip
    video = await index_of(media["video"])
    side = SidecarAudio("file:1", 1, await index_of(tagged), "rus", "x")
    vf = VirtualFile(build(video, [], [], [side]), reader({"video": media["video"], "file:1": tagged}))
    out2 = tmp_path / "virt2.mkv"
    out2.write_bytes(b"".join([c async for c in vf.iter_range(0, vf.size - 1)]))
    assert [s for s in probe(str(out2))["streams"] if s["codec_type"] == "audio"][1]["tags"][
        "language"
    ] == "jpn"


async def test_virtual_audio_is_deterministic_across_ranges(media, tmp_path):
    vf, full, _ = await virtual(media, tmp_path)
    rnd = random.Random(7)
    for _ in range(60):
        a = rnd.randrange(vf.size)
        b = min(vf.size - 1, a + rnd.randrange(1, 300_000))
        assert b"".join([c async for c in vf.iter_range(a, b)]) == full[a : b + 1], (a, b)


async def test_incompatible_sidecars_are_rejected(media):
    video, side = await index_of(media["video"]), await index_of(media["mka"])
    with pytest.raises(AudioError, match="TimestampScale"):
        audio_mod.check_compatible(video, replace(side, timestamp_scale=side.timestamp_scale * 2))
    with pytest.raises(AudioError, match="Cues"):
        audio_mod.check_compatible(video, replace(side, cues=[]))
    assert "duration" in audio_mod.check_compatible(video, replace(side, duration=side.duration * 2))[0]


def test_patch_rejects_garbage_and_unknown_tracks(media):
    with pytest.raises(PatchError):
        patch_clusters(b"\x1f\x43\xb6\x75\x88garbage", {1: 3})
    data = media["mka"].read_bytes()
    side = asyncio.run(index_of(media["mka"]))
    span = audio_mod.spans(side)[1]
    chunk = data[span.src : span.src + span.length]
    with pytest.raises(PatchError, match="unexpected track"):
        patch_clusters(chunk, {7: 3})
    patched = patch_clusters(chunk, {1: 3})
    assert len(patched) == len(chunk) and patched != chunk


async def test_raw_stream_is_repacked_to_a_compatible_mka(media, tmp_path, http_url):
    out = convert.converted_path(tmp_path, 5)
    path = await asyncio.wait_for(convert.ensure_mka(http_url(media["ac3"]), out), 60)
    assert path == out and out.exists() and not out.with_name(out.name + ".part").exists()
    side = await index_of(path)
    assert audio_mod.check_compatible(await index_of(media["video"]), side) == []
    assert packets_md5(str(path), "0:a:0") == packets_md5(str(media["ac3"]), "0:a:0")  # -c copy: same frames
    again = convert.ensure_mka(http_url(media["ac3"]), out)  # cached: finished task / existing file
    assert await again == out
