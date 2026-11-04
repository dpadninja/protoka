"""End-to-end: a release with external audio (MKA + raw AC3), served with the tracks inside the stream."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from istok.mux import convert
from tests.conftest import connect_to_seed, file_index, logged
from tests.media import make_audio, make_video, needs_ffmpeg, packets_md5, probe

pytestmark = needs_ffmpeg
T = 90


@pytest.fixture
def release(make_seed, tmp_path: Path):
    m = tmp_path / "media"
    m.mkdir()
    files = {
        "[Grp] Title - 01.mkv": make_video(m / "e1.mkv", 12).read_bytes(),
        "RUS Sound/[Grp] Title - 01.mka": make_audio(m / "e1.mka", 12).read_bytes(),
        "Звук/[AniDUB]/Title - 01.ac3": make_audio(m / "e1.ac3", 12, codec="ac3", freq=550).read_bytes(),
        "Title - 02.avi": make_video(m / "e2.avi", 6, container="avi").read_bytes(),
        "RUS Sound/Title - 02.mka": make_audio(m / "e2.mka", 6, freq=440).read_bytes(),
    }
    return make_seed(files, top="Title"), m


async def start(client, ses, seed) -> list[dict]:
    async with asyncio.timeout(T):
        resp = await client.post("/create", json={"blob": seed.torrent.hex()})
        assert resp.status == 200
        connect_to_seed(ses, seed.info_hash, seed)
        return (await resp.json())["files"]


async def fetch(client, url: str, path: Path) -> bytes:
    async with asyncio.timeout(T):
        data = await (await client.get(url)).read()
    path.write_bytes(data)
    return data


def audio_streams(path: Path) -> list[dict]:
    return [s for s in probe(str(path))["streams"] if s["codec_type"] == "audio"]


async def test_mkv_gets_external_mka_and_repacked_ac3(client, torrent_session, release, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="istok")
    seed, media = release
    torrent_session.settings.save({"preferredAudioLang": "rus"})
    files = await start(client, torrent_session, seed)
    idx = file_index(files, "[Grp] Title - 01.mkv")
    out = tmp_path / "e1.mkv"
    await fetch(client, f"/{seed.info_hash}/{idx}", out)

    streams = audio_streams(out)
    assert [s["codec_name"] for s in streams] == ["aac", "flac", "ac3"]
    mux = logged(caplog, "mux")
    assert any(
        '+ audio rus "RUS Sound"' in m and "copied from .mka" in m and "default track" in m for m in mux
    )
    assert any("Title - 01.ac3 (repacked, build waited" in m for m in mux)
    repack = logged(caplog, "audio")
    assert repack[0].startswith("[Title · Title - 01.ac3] audio: repack started")
    assert " audio: repack done in " in repack[-1]
    # ffmpeg reading the raw stream from our own HTTP server is internal: not an "opened" line
    assert not any("Title - 01.ac3] stream: opened" in m for m in logged(caplog, "stream"))
    assert [s["tags"].get("language") for s in streams[1:]] == ["rus", "rus"]  # RUS Sound / Cyrillic folder
    assert [s["disposition"]["default"] for s in streams] == [0, 1, 0]  # the first Russian sidecar
    assert packets_md5(str(out), "0:a:1") == packets_md5(str(media / "e1.mka"), "0:a:0")
    assert packets_md5(str(out), "0:a:2") == packets_md5(str(media / "e1.ac3"), "0:a:0")

    info = await (await client.get(f"/{seed.info_hash}/{idx}/tracks.json")).json()
    status = {a["path"].split("/", 1)[1]: a["status"] for a in info["audio"]}
    assert status == {"RUS Sound/[Grp] Title - 01.mka": "mka", "Звук/[AniDUB]/Title - 01.ac3": "repacked"}
    assert all(r["rule"] for a in info["audio"] for r in a["match"]["reasons"])
    assert info["mux"]["kind"] == "virtual-mkv" and len(info["mux"]["audio"]) == 2

    # Range requests still see one consistent file
    async with asyncio.timeout(T):
        part = await (
            await client.get(f"/{seed.info_hash}/{idx}", headers={"Range": "bytes=5000-90000"})
        ).read()
    assert part == out.read_bytes()[5000:90001]


async def test_avi_gets_external_audio_through_ffmpeg(client, torrent_session, release, tmp_path):
    seed, _ = release
    torrent_session.settings.save({"preferredAudioLang": "rus"})
    files = await start(client, torrent_session, seed)
    idx = file_index(files, "Title - 02.avi")
    out = tmp_path / "e2.mkv"
    await fetch(client, f"/{seed.info_hash}/{idx}", out)
    streams = audio_streams(out)
    assert len(streams) == 2
    assert streams[1]["tags"]["language"] == "rus"
    assert [s["disposition"]["default"] for s in streams] == [0, 1]


async def test_repack_that_misses_the_wait_is_ready_next_time(client, torrent_session, release, tmp_path):
    seed, _ = release
    torrent_session.settings.save({"muxAudioWait": 0})
    files = await start(client, torrent_session, seed)
    idx = file_index(files, "[Grp] Title - 01.mkv")
    ac3 = file_index(files, "Title - 01.ac3")

    await fetch(client, f"/{seed.info_hash}/{idx}", tmp_path / "first.mkv")
    assert [s["codec_name"] for s in audio_streams(tmp_path / "first.mkv")] == [
        "aac",
        "flac",
    ]  # no wait → no ac3
    info = await (await client.get(f"/{seed.info_hash}/{idx}/tracks.json")).json()
    assert any(a["status"].startswith("repacking") for a in info["audio"])

    out = convert.converted_path(torrent_session.cache_path(seed.info_hash), ac3)
    await asyncio.wait_for(convert.ensure_mka("unused", out), T)  # the background repack finishes
    await torrent_session.remove(seed.info_hash)  # next playback = new engine, new build
    await start(client, torrent_session, seed)
    await fetch(client, f"/{seed.info_hash}/{idx}", tmp_path / "second.mkv")
    assert [s["codec_name"] for s in audio_streams(tmp_path / "second.mkv")] == ["aac", "flac", "ac3"]


async def test_stats_contract_still_holds(client, torrent_session, release):
    from tests.stremio_contract import validate

    seed, _ = release
    files = await start(client, torrent_session, seed)
    idx = file_index(files, "[Grp] Title - 01.mkv")
    async with asyncio.timeout(T):
        await (await client.get(f"/{seed.info_hash}/{idx}", headers={"Range": "bytes=0-100"})).read()
        stats = await (await client.get(f"/{seed.info_hash}/{idx}/stats.json")).json()
    assert validate(stats) == []
