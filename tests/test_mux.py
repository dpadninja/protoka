"""Virtual MKV: EBML primitives, index reading and layout, validated with ffmpeg on real files."""

from __future__ import annotations

import os
import random
from pathlib import Path

import pytest

from istok.mux import ebml
from istok.mux.layout import LayoutError, build
from istok.mux.mkv import MkvError, read_index
from istok.mux.virtual import VirtualFile
from istok.subs import fonts, names
from istok.subs.model import Match
from istok.subs.service import build_track
from tests.media import (
    FFMPEG,
    FFPROBE,
    ass_script,
    make_font,
    make_video,
    needs_ffmpeg,
    packets_md5,
    probe,
    srt_script,
    subtitle_dump,
)


# -- ebml ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("n", [0, 1, 126, 127, 128, 16382, 16383, 2**20, 2**35, 2**56 - 2])
def test_size_vint_roundtrip(n):
    enc = ebml.encode_size(n)
    assert ebml.read_size(enc, 0) == (n, len(enc))
    fixed = ebml.encode_size(n, 8)
    assert ebml.read_size(fixed, 0) == (n, 8)


def test_unknown_size_and_ids():
    assert ebml.read_size(b"\x01\xff\xff\xff\xff\xff\xff\xff", 0) == (ebml.UNKNOWN, 8)
    assert ebml.read_id(ebml.encode_id(ebml.ID.SEGMENT), 0) == (ebml.ID.SEGMENT, 4)
    el = ebml.master(ebml.ID.SEEK, ebml.uint(ebml.ID.SEEK_POSITION, 300, 8))
    (h,) = list(ebml.iter_children(el))
    assert h.id == ebml.ID.SEEK
    (c,) = list(ebml.iter_children(el, h.data_offset, h.end))
    assert ebml.read_uint(el[c.data_offset : c.end]) == 300


def test_block_header_range():
    assert ebml.block_header(3, -5) == b"\x83\xff\xfb\x00"
    with pytest.raises(ebml.EbmlError):
        ebml.block_header(3, 40000)


# -- helpers -----------------------------------------------------------------------------------------
def file_reader(path: Path):
    data = path.read_bytes()

    async def read(offset: int, n: int) -> bytes:
        return data[offset : offset + n]

    async def source(key: str, start: int, end: int):
        assert key == "video", key
        yield data[start : end + 1]

    return data, read, source


def tracks_for(tmp: Path):
    video = names.parse_file(0, "T/Title - 01.mkv")
    ass_text = ass_script(
        [
            ("0:00:01.00", "0:00:03.00", "Первая реплика"),
            ("0:00:20.50", "0:00:23.00", "{\\fnOther Font}На двадцатой, с запятой"),
            ("0:00:28.00", "0:00:29.50", "Последняя"),
        ]
    )
    srt_text = srt_script(
        [("00:00:02,000", "00:00:04,000", "SRT one"), ("00:00:21,000", "00:00:22,500", "SRT at 21")]
    )
    return [
        build_track(
            Match(video, names.parse_file(1, "T/RUS Subs/Title - 01.ass"), []), ass_text.encode("cp1251")
        ),
        build_track(Match(video, names.parse_file(2, "T/Title - 01.eng.srt"), []), srt_text.encode()),
    ]


async def write_virtual(src: Path, out: Path, tracks, faces) -> tuple[VirtualFile, bytes]:
    _, read, source = file_reader(src)
    index = await read_index(read, src.stat().st_size)
    vf = VirtualFile(build(index, tracks, faces), source)
    data = b"".join([c async for c in vf.iter_range(0, vf.size - 1)])
    out.write_bytes(data)
    return vf, data


@pytest.fixture(scope="module")
def mkv(tmp_path_factory) -> Path:
    if not (FFMPEG and FFPROBE):
        pytest.skip("ffmpeg not installed")
    return make_video(tmp_path_factory.mktemp("media") / "src.mkv", 30)


# -- virtual MKV, checked by ffmpeg ----------------------------------------------------------------
@needs_ffmpeg
async def test_virtual_mkv_tracks_seek_and_untouched_video(mkv: Path, tmp_path: Path):
    face = fonts.make_face("9", "Fonts/TestSans.ttf", make_font("Test Sans"))
    out = tmp_path / "virt.mkv"
    vf, _ = await write_virtual(mkv, out, tracks_for(tmp_path), [face])

    streams = probe(str(out))["streams"]
    subs = [s for s in streams if s["codec_type"] == "subtitle"]
    assert [(s["codec_name"], s["tags"]["language"]) for s in subs] == [("ass", "rus"), ("subrip", "eng")]
    assert subs[0]["tags"]["title"] == "RUS Subs"
    (att,) = [s for s in streams if s["codec_type"] == "attachment"]
    assert att["tags"]["filename"] == "TestSans.ttf"
    assert att["tags"]["mimetype"] == "application/x-truetype-font"

    # whole-track extraction and extraction after a seek (via the rebuilt Cues)
    assert "Первая реплика" in subtitle_dump(str(out), 0, fmt="ass")
    after_seek = subtitle_dump(str(out), 1, seek=20)
    assert "SRT at 21" in after_seek and "SRT one" not in after_seek
    assert "На двадцатой, с запятой" in subtitle_dump(str(out), 0, seek=20, fmt="ass")

    # video and audio packets are byte-identical to the source
    assert packets_md5(str(out), "0:v") == packets_md5(str(mkv), "0:v")
    assert packets_md5(str(out), "0:a") == packets_md5(str(mkv), "0:a")
    assert vf.layout.summary["kind"] == "virtual-mkv"


@needs_ffmpeg
async def test_virtual_mkv_is_deterministic_and_range_consistent(mkv: Path, tmp_path: Path):
    tracks = tracks_for(tmp_path)
    vf, full = await write_virtual(mkv, tmp_path / "a.mkv", tracks, [])
    _, again = await write_virtual(mkv, tmp_path / "b.mkv", tracks, [])
    assert full == again  # same input → same bytes (players issue many independent Range requests)
    rnd = random.Random(1)
    for _ in range(50):
        a = rnd.randrange(vf.size)
        b = min(vf.size - 1, a + rnd.randrange(1, 200_000))
        part = b"".join([c async for c in vf.iter_range(a, b)])
        assert part == full[a : b + 1], (a, b)


@needs_ffmpeg
async def test_virtual_mkv_existing_attachments_and_no_tracks(mkv: Path, tmp_path: Path):
    """A source that already has an attachment keeps it; adding only fonts (no events) still works."""
    import subprocess

    src = tmp_path / "with_att.mkv"
    font_file = tmp_path / "Old.ttf"
    font_file.write_bytes(make_font("Old Font"))
    subprocess.run(
        [FFMPEG, "-loglevel", "error", "-y", "-i", str(mkv), "-c", "copy", "-attach", str(font_file),
         "-metadata:s:t:0", "mimetype=application/x-truetype-font", "-cluster_time_limit", "1000", str(src)],
        check=True,
    )  # fmt: skip
    face = fonts.make_face("9", "New.ttf", make_font("New Font"))
    out = tmp_path / "virt.mkv"
    await write_virtual(src, out, tracks_for(tmp_path)[:1], [face])
    names_ = sorted(
        s["tags"]["filename"] for s in probe(str(out))["streams"] if s["codec_type"] == "attachment"
    )
    assert names_ == ["New.ttf", "Old.ttf"]


@needs_ffmpeg
async def test_no_cues_is_rejected(mkv: Path, tmp_path: Path):
    _, read, _ = file_reader(mkv)
    index = await read_index(read, mkv.stat().st_size)
    index.cues = []
    with pytest.raises(LayoutError):
        build(index, tracks_for(tmp_path), [])


async def test_not_mkv_is_rejected(tmp_path: Path):
    data = b"RIFF" + os.urandom(1000)

    async def read(offset: int, n: int) -> bytes:
        return data[offset : offset + n]

    with pytest.raises(MkvError):
        await read_index(read, len(data))


async def test_client_disconnect_does_not_break_the_shared_build():
    """A request cancelled mid-build must not leave the cache entry hanging for everyone else."""
    import asyncio
    from types import SimpleNamespace

    from istok.mux.service import MuxService

    started, release = asyncio.Event(), asyncio.Event()
    sentinel = object()

    class Service(MuxService):
        async def _build(self, engine, idx, subs, deadline):
            started.set()
            await release.wait()
            return sentinel

    engine = SimpleNamespace(
        info_hash="ab" * 20,
        mux_cache={},
        subs=SimpleNamespace(matches=lambda idx: ["a match"], audio_matches=lambda idx: [], warnings=[]),
    )
    svc = Service(enabled=lambda: True, source_url=lambda ih, idx: "")
    first = asyncio.create_task(svc.open(engine, 0))
    await started.wait()
    first.cancel()  # the player went away
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(svc.open(engine, 0))
    release.set()
    assert await asyncio.wait_for(second, 5) is sentinel


async def test_build_errors_never_leave_waiters_hanging():
    """Regression: an exception before the build starts (here: a broken subs object) used to skip
    `entry.done.set()`, so every request for that file waited forever."""
    import asyncio
    from types import SimpleNamespace

    from istok.mux.service import MuxService

    def broken(idx):
        raise RuntimeError("boom")

    engine = SimpleNamespace(info_hash="ab" * 20, mux_cache={})
    engine.subs = SimpleNamespace(matches=lambda idx: ["m"], audio_matches=broken, warnings=[])
    svc = MuxService(enabled=lambda: True, source_url=lambda ih, idx: "")
    original = svc._timeout_for
    calls = []

    def timeout_for(subs, idx):
        calls.append(idx)
        return original(subs, idx)

    svc._timeout_for = timeout_for  # type: ignore[method-assign]
    assert await asyncio.wait_for(svc.open(engine, 0), 5) is None
    assert calls == [0]
    assert "boom" in svc.status(engine, 0)["reason"]
