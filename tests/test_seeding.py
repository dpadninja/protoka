"""End-to-end seeding: cached torrents keep seeding, only what is watched downloads, eviction, next episode."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import libtorrent as lt

from tests.conftest import OFFLINE, Seed, connect_to_seed, file_index, logged
from tests.stremio_contract import validate

T = 60


async def watch(client, ses, seed: Seed, name: str) -> int:
    """Open the torrent like Stremio, read a whole file through the HTTP stream, return its index."""
    async with asyncio.timeout(T):
        resp = await client.post("/create", json={"blob": seed.torrent.hex()})
        connect_to_seed(ses, seed.info_hash, seed)
        files = (await resp.json())["files"]
        idx = file_index(files, name)
        data = await (await client.get(f"/{seed.info_hash}/{idx}")).read()
    assert data == seed.content(name)
    return idx


async def stop_watching(client, ses, ih: str) -> None:
    await client.get(f"/{ih}/remove")  # what Stremio does when the player closes
    await ses.library.tick()


def leecher(tmp_path: Path, torrent: bytes) -> tuple[lt.session, lt.torrent_handle]:
    """An unrelated BitTorrent client that wants the whole torrent."""
    ses = lt.session({**OFFLINE, "alert_mask": 0})
    atp = lt.add_torrent_params()
    atp.ti = lt.torrent_info(torrent)
    atp.save_path = str(tmp_path / "leecher")
    return ses, ses.add_torrent(atp)


async def test_watched_file_is_seeded_to_others_after_playback(client, torrent_session, make_seed, tmp_path):
    seed = make_seed({"Movie.mkv": os.urandom(400_000)}, top="Movie")
    idx = await watch(client, torrent_session, seed, "Movie.mkv")
    await stop_watching(client, torrent_session, seed.info_hash)
    engine = torrent_session.get(seed.info_hash)
    assert engine.state == "seeding"
    flags = engine.handle.flags()
    assert flags & lt.torrent_flags.auto_managed and flags & lt.torrent_flags.upload_mode
    seed.session.pause()  # the original seeder is gone: we are the only source now

    other, handle = leecher(tmp_path, seed.torrent)
    try:
        handle.connect_peer(("127.0.0.1", torrent_session.listen_port))
        deadline = time.monotonic() + T
        while not handle.status().is_seeding and time.monotonic() < deadline:
            connect_and_wait = handle.status().num_peers == 0
            if connect_and_wait:
                handle.connect_peer(("127.0.0.1", torrent_session.listen_port))
            await asyncio.sleep(0.2)
        assert handle.status().is_seeding, "the other client could not download from us"
        got = (tmp_path / "leecher" / "Movie" / "Movie.mkv").read_bytes()
        assert got == seed.content("Movie.mkv")
        for _ in range(50):  # libtorrent publishes transfer counters about once a second
            report = await (await client.get("/seeding.json")).json()
            (t,) = report["torrents"]
            if t["uploaded"] >= len(got):
                break
            await asyncio.sleep(0.2)
        assert t["state"] == "seeding" and t["uploaded"] >= len(got) and t["reasons"]
        assert idx == 0
    finally:
        other.pause()


async def test_an_abandoned_file_is_not_completed_but_seeded_and_resumes_when_watched_again(
    client, torrent_session, make_seed, caplog
):
    caplog.set_level(logging.INFO, logger="protoka")
    a = make_seed({"A.mkv": os.urandom(900_000)}, top="A")
    # throttled seeder: the whole file would take ~45 s, so "left after the first bytes" is real
    a.session.get_torrents()[0].set_upload_limit(20_000)
    async with asyncio.timeout(T):
        await client.post("/create", json={"blob": a.torrent.hex()})
        connect_to_seed(torrent_session, a.info_hash, a)
        await (await client.get(f"/{a.info_hash}/0", headers={"Range": "bytes=0-1000"})).read()
    ea = torrent_session.get(a.info_hash)
    await stop_watching(client, torrent_session, a.info_hash)
    assert ea.state == "seeding" and ea.handle.flags() & lt.torrent_flags.upload_mode
    await asyncio.sleep(1)  # blocks requested before the switch may still arrive
    left = ea.remaining_bytes()
    assert left > 0
    await asyncio.sleep(2)  # the seeder is still there, but we do not fetch
    assert ea.remaining_bytes() == left

    torrent_session.stream_opened(ea)  # the player is back
    await torrent_session.library.tick()
    assert ea.state == "streaming" and not ea.handle.flags() & lt.torrent_flags.upload_mode
    async with asyncio.timeout(T):
        while ea.remaining_bytes() == left:
            await asyncio.sleep(0.1)
    torrent_session.stream_closed(ea)
    a.session.pause()
    assert any(
        m.startswith("[A] library: streaming → seeding (not watched: seeding what is here, not fetching ")
        for m in logged(caplog, "library")
    )


async def test_next_episode_downloads_whole_once_the_watched_one_is_on_disk(
    client, torrent_session, make_seed, caplog
):
    caplog.set_level(logging.INFO, logger="protoka")
    seed = make_seed(
        {
            "[G] Title - 01.mkv": os.urandom(500_000),
            "[G] Title - 02.mkv": os.urandom(500_000),
            "[G] Title - 03.mkv": os.urandom(500_000),
            "Subs/[G] Title - 02.srt": b"1\n00:00:01,000 --> 00:00:02,000\nhi\n",
        },
        top="T",
    )
    prepared: list[int] = []
    torrent_session.library.on_prefetched = lambda e, idx: prepared.append(idx)
    async with asyncio.timeout(T):
        resp = await client.post("/create", json={"blob": seed.torrent.hex()})
        files = (await resp.json())["files"]
        connect_to_seed(torrent_session, seed.info_hash, seed)
        ep1, ep2 = file_index(files, "[G] Title - 01.mkv"), file_index(files, "[G] Title - 02.mkv")
        stream = await client.get(f"/{seed.info_hash}/{ep1}")
        await stream.read()  # episode 1 downloaded; pretend the player is still open
    engine = torrent_session.get(seed.info_hash)
    torrent_session.stream_opened(engine)
    decisions = {d.ih: d for d in await torrent_session.library.tick()}
    d = decisions[seed.info_hash]
    assert d.state.value == "streaming" and d.prefetch
    target = torrent_session.library.prefetch[seed.info_hash]
    srt = file_index(files, "[G] Title - 02.srt")
    assert target.video_idx == ep2 and target.files == (ep2, srt)
    assert engine.handle.file_priority(ep2) == 1  # below the episode being watched

    deadline = time.monotonic() + T
    while time.monotonic() < deadline:
        progress = engine.handle.file_progress()
        if progress[ep2] == files[ep2]["length"] and progress[srt] == files[srt]["length"]:
            break
        await asyncio.sleep(0.1)
    progress = engine.handle.file_progress()
    assert (
        progress[ep2] == files[ep2]["length"] and progress[srt] == files[srt]["length"]
    )  # whole, in real files
    assert progress[file_index(files, "[G] Title - 03.mkv")] == 0  # one episode ahead only
    await torrent_session.library.tick()
    await torrent_session.library.tick()
    torrent_session.stream_closed(engine)
    assert prepared == [ep2]  # its audio is prepared ahead (none here)

    prefetch = logged(caplog, "prefetch")
    assert prefetch[0].startswith(
        '[T · [G] Title - 01.mkv] prefetch: next episode is "[G] Title - 02.mkv" — video + 1 subtitle(s), '
    )
    assert prefetch[-1].startswith("[T · [G] Title - 02.mkv] prefetch: done in ")
    assert sum("prefetch: started" in m for m in prefetch) == 1


async def test_our_own_ffmpeg_reading_a_file_is_not_someone_watching(client, torrent_session, seed):
    await watch(client, torrent_session, seed, "Show.S01E01.mkv")
    e = torrent_session.get(seed.info_hash)
    await stop_watching(client, torrent_session, seed.info_hash)
    async with asyncio.timeout(T):
        before = e.last_activity
        resp = await client.get(f"/{seed.info_hash}/0?mux=0", headers={"User-Agent": "Lavf/61.7.100"})
        assert resp.status == 200 and await resp.read()
    assert e.last_activity == before and e.open_streams == 0
    await torrent_session.library.tick()
    assert e.state == "seeding"


async def test_a_repack_started_while_watching_finishes_after_the_viewer_left(
    client, torrent_session, make_seed
):
    """Our ffmpeg reads a file that is not downloaded yet; the viewer is gone. The torrent keeps fetching for it
    (otherwise ffmpeg would hang and block the repack queue), without counting as someone watching."""
    seed = make_seed({"A.mkv": os.urandom(300_000), "A.ac3": os.urandom(400_000)}, top="A")
    async with asyncio.timeout(T):
        await client.post("/create", json={"blob": seed.torrent.hex()})
        e = torrent_session.get(seed.info_hash)
        await stop_watching(client, torrent_session, seed.info_hash)
        assert e.state == "seeding"
        ac3 = file_index(e.files, "A.ac3")
        resp = await client.get(f"/{seed.info_hash}/{ac3}?mux=0", headers={"User-Agent": "Lavf/61.7.100"})
        connect_to_seed(torrent_session, seed.info_hash, seed)
        assert await resp.read() == seed.content("A.ac3")
    assert e.open_streams == 0
    await torrent_session.library.tick()
    assert e.state == "seeding" and e.internal_reads == 0


async def test_cache_full_evicts_the_least_recently_watched(client, torrent_session, make_seed, caplog):
    caplog.set_level(logging.INFO, logger="protoka")
    old = make_seed({"Old.mkv": os.urandom(700_000)}, top="Old")
    new = make_seed({"New.mkv": os.urandom(700_000)}, top="New")
    await watch(client, torrent_session, old, "Old.mkv")
    await stop_watching(client, torrent_session, old.info_hash)
    torrent_session.get(old.info_hash).last_watched = time.time() - 2 * 86400  # beyond CACHE_KEEP_RECENT
    torrent_session.settings.update({"cacheSize": 1_000_000})  # room for one of them
    torrent_session.library._sizes_at = 0  # rescan disk usage now

    await watch(client, torrent_session, new, "New.mkv")
    torrent_session.library._sizes_at = 0
    await torrent_session.library.tick()
    assert old.info_hash not in torrent_session.engines  # evicted
    assert not torrent_session.cache_path(old.info_hash).exists()
    assert new.info_hash in torrent_session.engines  # the one being watched is never touched
    report = await (await client.get("/seeding.json")).json()
    assert report["recentlyEvicted"][0]["infoHash"] == old.info_hash
    assert "least recently watched" in report["recentlyEvicted"][0]["reason"]

    # the cache size is logged on the first tick, after an eviction, then only every CACHE_LOG_EVERY
    sizes = [m for m in logged(caplog, "cache") if m.startswith("cache: ")]
    assert sizes[0].startswith("cache: ") and " of 976.56 KB (" in sizes[-1] and "torrent(s) [" in sizes[-1]
    assert "free on disk (min " in sizes[-1]
    before = len(sizes)
    await torrent_session.library.tick()
    assert len([m for m in logged(caplog, "cache") if m.startswith("cache: ")]) == before


async def test_cache_full_keeps_what_was_just_watched_and_says_so_once(
    client, torrent_session, make_seed, caplog
):
    caplog.set_level(logging.INFO, logger="protoka")
    old = make_seed({"Old.mkv": os.urandom(700_000)}, top="Old")
    new = make_seed({"New.mkv": os.urandom(700_000)}, top="New")
    await watch(client, torrent_session, old, "Old.mkv")
    await stop_watching(client, torrent_session, old.info_hash)
    torrent_session.settings.update({"cacheSize": 1_000_000})
    await watch(client, torrent_session, new, "New.mkv")
    for _ in range(3):
        torrent_session.library._sizes_at = 0
        await torrent_session.library.tick()
    assert old.info_hash in torrent_session.engines  # watched a minute ago: over CACHE_SIZE, but kept
    over = [m for m in logged(caplog, "cache") if "over CACHE_SIZE" in m]
    assert len(over) == 1 and "kept" in over[0]
    report = await (await client.get("/seeding.json")).json()
    assert report["cache"]["overBy"] > 0 and report["cache"]["keepRecent"] == 86400

    torrent_session.get(old.info_hash).last_watched = time.time() - 2 * 86400
    torrent_session.library._sizes_at = 0
    await torrent_session.library.tick()
    assert old.info_hash not in torrent_session.engines
    assert "cache: within CACHE_SIZE again" in logged(caplog, "cache")


async def test_background_upload_is_shared_while_watching(client, torrent_session, make_seed):
    torrent_session.settings.update({"streamBgUploadLimit": 200_000})
    a = make_seed({"A.mkv": os.urandom(300_000)}, top="A")
    b = make_seed({"B.mkv": os.urandom(300_000)}, top="B")
    await watch(client, torrent_session, a, "A.mkv")
    await stop_watching(client, torrent_session, a.info_hash)
    ea = torrent_session.get(a.info_hash)
    assert ea.handle.upload_limit() <= 0  # nobody watches: no per-torrent limit

    await watch(client, torrent_session, b, "B.mkv")
    eb = torrent_session.get(b.info_hash)
    torrent_session.stream_opened(eb)
    await torrent_session.library.tick()
    assert ea.handle.upload_limit() == 200_000  # the only background torrent gets the whole share
    assert eb.handle.upload_limit() <= 0  # the watched one: unlimited (reciprocation)
    torrent_session.stream_closed(eb)
    await stop_watching(client, torrent_session, b.info_hash)
    assert ea.handle.upload_limit() <= 0


async def test_restored_torrents_start_gradually_without_recheck(
    client, torrent_session, make_seed, settings
):
    from protoka.session import TorrentSession

    seed = make_seed({"Movie.mkv": os.urandom(300_000)}, top="Movie")
    await watch(client, torrent_session, seed, "Movie.mkv")
    await torrent_session.close()  # writes resume data
    restarted = TorrentSession(settings, overrides=OFFLINE, default_trackers=False)
    await restarted.start()
    try:
        engine = restarted.get(seed.info_hash)
        assert engine is not None
        st = engine.handle.status()
        assert st.state != lt.torrent_status.states.checking_files  # resume data trusted, no recheck
        assert st.has_metadata
        deadline = time.monotonic() + 10
        while restarted.library.startup_pending and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        await restarted.library.tick()
        assert engine.state == "seeding"
        assert engine.handle.file_progress()[0] == len(seed.content("Movie.mkv"))
    finally:
        await restarted.close()


async def test_seeding_report_and_stats_contract(client, torrent_session, make_seed):
    seed = make_seed({"Movie.mkv": os.urandom(200_000)}, top="Movie")
    idx = await watch(client, torrent_session, seed, "Movie.mkv")
    stats = await (await client.get(f"/{seed.info_hash}/{idx}/stats.json")).json()
    assert validate(stats) == []
    await torrent_session.library.tick()
    report = await (await client.get("/seeding.json")).json()
    assert {"cache", "nextEpisode", "limits", "torrents", "recentlyEvicted"} <= set(report)
    assert report["cache"]["budget"] == torrent_session.settings["cacheSize"]
    (t,) = report["torrents"]
    assert t["state"] == "streaming" and t["sizeOnDisk"] > 0
