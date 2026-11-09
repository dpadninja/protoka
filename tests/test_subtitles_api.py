"""End-to-end: a torrent with sidecar subtitles and a fonts folder, served with them embedded."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from protoka.subs.fonts import families
from tests.conftest import connect_to_seed, file_index, logged
from tests.media import (
    ass_script,
    make_font,
    make_video,
    needs_ffmpeg,
    packets_md5,
    probe,
    srt_script,
    subtitle_dump,
    write_fallback_fonts,
)

pytestmark = needs_ffmpeg
T = 60


@pytest.fixture
def anime(make_seed, tmp_path: Path):
    media = tmp_path / "media"
    media.mkdir()
    ep1 = make_video(media / "ep1.mkv", 12).read_bytes()
    ep2 = make_video(media / "ep2.mkv", 6).read_bytes()
    ep3 = make_video(media / "ep3.avi", 4, container="avi").read_bytes()
    files = {
        "[Grp] Title - 01 [ABCD1234].mkv": ep1,
        "[Grp] Title - 02 [BCDE2345].mkv": ep2,
        "Title - 03.avi": ep3,
        "RUS Subs/[Grp] Title - 01.ass": ass_script(
            [
                ("0:00:01.00", "0:00:03.00", "Первая реплика"),
                ("0:00:08.00", "0:00:10.00", "Реплика на восьмой"),
            ]
        ).encode("cp1251"),
        "RUS Subs/[Grp] Title - 02.ass": b"[Script Info]\nbroken: yes\n",  # no [Events]
        "Title - 03.srt": srt_script([("00:00:01,000", "00:00:02,000", "AVI line")]).encode(),
        "Fonts/TestSans.ttf": make_font("Test Sans"),
        "Fonts/Unused.ttf": make_font("Unused Font"),
    }
    return make_seed(files, top="Title")


async def start(client, ses, seed) -> list[dict]:
    async with asyncio.timeout(T):
        resp = await client.post("/create", json={"blob": seed.torrent.hex()})
        assert resp.status == 200
        connect_to_seed(ses, seed.info_hash, seed)
        return (await resp.json())["files"]


async def download(client, url: str, path: Path, **kw) -> dict:
    async with asyncio.timeout(T):
        resp = await client.get(url, **kw)
        data = await resp.read()
    path.write_bytes(data)
    return {"status": resp.status, "headers": resp.headers, "data": data}


async def test_subtitles_json_explains_matches(client, torrent_session, anime):
    files = await start(client, torrent_session, anime)
    idx = file_index(files, "[Grp] Title - 01 [ABCD1234].mkv")
    info = await (await client.get(f"/{anime.info_hash}/{idx}/subtitles.json")).json()
    (sub,) = info["subtitles"]
    assert sub["path"] == "Title/RUS Subs/[Grp] Title - 01.ass"
    assert sub["lang"] == "rus" and sub["format"] == "ass"
    rules = {r["rule"] for r in sub["match"]["reasons"]}
    assert {"same_episode", "same_series"} <= rules and "different_episode" not in rules
    assert [f["path"] for f in info["fonts"]] == ["Title/Fonts/TestSans.ttf", "Title/Fonts/Unused.ttf"]
    assert info["mux"] is None  # nothing built yet


async def test_mkv_is_served_with_subtitles_and_needed_fonts(
    client, torrent_session, anime, tmp_path, caplog
):
    caplog.set_level(logging.INFO, logger="protoka")
    files = await start(client, torrent_session, anime)
    idx = file_index(files, "[Grp] Title - 01 [ABCD1234].mkv")
    url = f"/{anime.info_hash}/{idx}"
    out = tmp_path / "muxed.mkv"
    r = await download(client, url, out)
    assert r["status"] == 200
    assert r["headers"]["Content-Type"] == "video/x-matroska"
    assert (
        int(r["headers"]["Content-Length"])
        == len(r["data"])
        > len(anime.content("[Grp] Title - 01 [ABCD1234].mkv"))
    )

    streams = probe(str(out))["streams"]
    (s,) = [x for x in streams if x["codec_type"] == "subtitle"]
    assert (s["codec_name"], s["tags"]["language"]) == ("ass", "rus")
    attachments = [x["tags"]["filename"] for x in streams if x["codec_type"] == "attachment"]
    assert attachments == ["TestSans.ttf"]  # only the font the script uses
    assert "Реплика на восьмой" in subtitle_dump(str(out), 0, seek=7, fmt="ass")

    original = tmp_path / "orig.mkv"
    original.write_bytes(anime.content("[Grp] Title - 01 [ABCD1234].mkv"))
    assert packets_md5(str(out), "0:v") == packets_md5(str(original), "0:v")

    # Range requests see the same virtual file; ?mux=0 gives the original bytes
    part = await download(client, url, tmp_path / "part", headers={"Range": "bytes=1000-50000"})
    assert part["status"] == 206 and part["data"] == r["data"][1000:50001]
    assert part["headers"]["Content-Range"] == f"bytes 1000-50000/{len(r['data'])}"
    raw = await download(client, url + "?mux=0", tmp_path / "raw")
    assert raw["data"] == anime.content("[Grp] Title - 01 [ABCD1234].mkv")

    info = await (await client.get(f"{url}/subtitles.json")).json()
    assert info["mux"]["kind"] == "virtual-mkv"
    assert info["mux"]["attachments"] == ["TestSans.ttf"]

    # the log tells the whole story of the build, once, naming the torrent and the file (not the info hash)
    tag = "[Title · [Grp] Title - 01 [ABCD1234].mkv]"
    mux = logged(caplog, "mux")
    assert mux[0].startswith(f"{tag} mux: building — found 1 subtitle(s)")
    assert any(
        '+ subtitle rus "RUS Subs" ← Title/RUS Subs/[Grp] Title - 01.ass (ASS, 2 events' in m for m in mux
    )
    assert any("+ fonts TestSans.ttf" in m for m in mux)
    assert " mux: ready in " in mux[-1] and "→ virtual MKV" in mux[-1]
    (opened,) = logged(caplog, "stream")  # several requests, one INFO line
    assert opened.startswith(f"{tag} stream: opened → virtual MKV") and "1 subtitle track(s) added" in opened
    assert not any(
        anime.info_hash in r.getMessage() for r in caplog.records if r.name.startswith("protoka.mux")
    )


async def test_broken_subtitle_falls_back_to_original(client, torrent_session, anime, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="protoka")
    files = await start(client, torrent_session, anime)
    idx = file_index(files, "[Grp] Title - 02 [BCDE2345].mkv")
    r = await download(client, f"/{anime.info_hash}/{idx}", tmp_path / "ep2.mkv")
    assert r["status"] == 200 and r["data"] == anime.content("[Grp] Title - 02 [BCDE2345].mkv")
    info = await (await client.get(f"/{anime.info_hash}/{idx}/subtitles.json")).json()
    assert info["mux"]["kind"] == "original"
    assert any("no [Events]" in w for w in info["warnings"])
    mux = logged(caplog, "mux")
    assert any("- subtitle Title/RUS Subs/[Grp] Title - 02.ass: no [Events]" in m for m in mux)
    assert "→ original file: no usable subtitles or audio" in mux[-1]


async def test_avi_is_remuxed_with_ffmpeg(client, torrent_session, anime, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="protoka")
    files = await start(client, torrent_session, anime)
    idx = file_index(files, "Title - 03.avi")
    out = tmp_path / "ep3.mkv"
    r = await download(client, f"/{anime.info_hash}/{idx}", out)
    assert r["status"] == 200
    assert r["headers"]["Content-Type"] == "video/x-matroska"
    assert "Content-Length" not in r["headers"] and "Accept-Ranges" not in r["headers"]
    (s,) = [x for x in probe(str(out))["streams"] if x["codec_type"] == "subtitle"]
    assert s["codec_name"] == "subrip"
    assert "AVI line" in subtitle_dump(str(out), 0)
    assert "→ ffmpeg remux" in logged(caplog, "mux")[-1]


async def test_subtitle_and_font_endpoints(client, torrent_session, anime):
    files = await start(client, torrent_session, anime)
    idx = file_index(files, "[Grp] Title - 01 [ABCD1234].mkv")
    sid = file_index(files, "[Grp] Title - 01.ass")
    base = f"/{anime.info_hash}/{idx}/subtitles/{sid}"
    async with asyncio.timeout(T):
        ass_text = await (await client.get(f"{base}.ass")).text()
        assert "Первая реплика" in ass_text and ass_text.startswith("[Script Info]")  # cp1251 → UTF-8
        srt = await client.get(f"{base}.srt")
        assert srt.headers["Content-Type"].startswith("application/x-subrip")
        assert "00:00:08,000 --> 00:00:10,000\nРеплика на восьмой" in await srt.text()
        vtt = await (await client.get(f"{base}.vtt")).text()
        assert vtt.startswith("WEBVTT")
        assert (await client.get(f"/{anime.info_hash}/{idx}/subtitles/{sid + 999}.srt")).status == 404

        font = await client.get(f"/{anime.info_hash}/fonts/{file_index(files, 'TestSans.ttf')}")
        assert font.status == 200
        assert "test sans" in families(await font.read())  # (font bytes embed a timestamp)
        for not_a_font in (
            file_index(files, "Title - 03.srt"),
            "9999",
            "1/x.ttf",
        ):
            assert (await client.get(f"/{anime.info_hash}/fonts/{not_a_font}")).status == 404


async def test_mux_can_be_disabled(client, torrent_session, anime, tmp_path):
    torrent_session.settings.update({"subtitlesMux": False})
    files = await start(client, torrent_session, anime)
    idx = file_index(files, "[Grp] Title - 01 [ABCD1234].mkv")
    r = await download(client, f"/{anime.info_hash}/{idx}", tmp_path / "x")
    assert r["data"] == anime.content("[Grp] Title - 01 [ABCD1234].mkv")


async def test_missing_fonts_are_replaced_by_bundled_ones(
    client, torrent_session, make_seed, tmp_path, caplog
):
    caplog.set_level(logging.INFO, logger="protoka")
    torrent_session.settings.update({"fallbackFontsDir": str(write_fallback_fonts(tmp_path / "bundle"))})
    media = tmp_path / "media"
    media.mkdir()
    script = ass_script(
        [
            ("0:00:01.00", "0:00:03.00", "Первая реплика"),
            ("0:00:04.00", "0:00:06.00", r"{\fnFoo Serif Bold}Вывеска"),
        ],
        font="Arial",
    )
    seed = make_seed(
        {
            "[G] Show - 01.mkv": make_video(media / "e.mkv", 6).read_bytes(),
            "[G] Show - 01.ass": script.encode(),
        },
        top="Show",
    )
    files = await start(client, torrent_session, seed)
    idx = file_index(files, "[G] Show - 01.mkv")
    out = tmp_path / "out.mkv"
    r = await download(client, f"/{seed.info_hash}/{idx}", out)
    assert r["status"] == 200

    streams = probe(str(out))["streams"]
    attached = sorted(x["tags"]["filename"].split(".")[0] for x in streams if x["codec_type"] == "attachment")
    assert attached == ["LiberationSans-Regular", "NotoSerif-Bold"]
    dumped = subtitle_dump(str(out), 0, fmt="ass")
    assert "Style: Default,Liberation Sans," in dumped and "\\fnNoto Serif\\b1}Вывеска" in dumped

    info = await (await client.get(f"/{seed.info_hash}/{idx}/subtitles.json")).json()
    by = {f["font"]: f for f in info["fontFallback"]}
    assert by["arial"]["substitute"] == "Liberation Sans" and any(
        "metric twin" in x for x in by["arial"]["reasons"]
    )
    assert by["foo serif bold"]["substitute"] == "Noto Serif" and by["foo serif bold"]["faces"] == ["Bold"]
    mux = logged(caplog, "mux")
    assert any('~ font "arial" → "Liberation Sans" Regular' in m for m in mux)
    assert any("+ fonts LiberationSans-Regular.subset-" in m for m in mux)


async def test_font_fallback_can_be_disabled(client, torrent_session, make_seed, tmp_path):
    torrent_session.settings.update(
        {"fallbackFontsDir": str(write_fallback_fonts(tmp_path / "bundle")), "fontFallback": False}
    )
    media = tmp_path / "media"
    media.mkdir()
    script = ass_script([("0:00:01.00", "0:00:03.00", "Текст")], font="Arial")
    seed = make_seed(
        {"Show - 01.mkv": make_video(media / "e.mkv", 4).read_bytes(), "Show - 01.ass": script.encode()},
        top="S",
    )
    files = await start(client, torrent_session, seed)
    idx = file_index(files, "Show - 01.mkv")
    out = tmp_path / "out.mkv"
    await download(client, f"/{seed.info_hash}/{idx}", out)
    assert not [x for x in probe(str(out))["streams"] if x["codec_type"] == "attachment"]
    assert "Style: Default,Arial," in subtitle_dump(str(out), 0, fmt="ass")
