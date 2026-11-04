"""End-to-end seeding: cached torrents keep seeding, background work yields to watching, eviction, prefetch."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import libtorrent as lt

from istok.stream import read_file
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


async def test_background_completion_yields_to_what_is_being_watched(
    client, torrent_session, make_seed, caplog
):
    caplog.set_level(logging.INFO, logger="istok")
    a = make_seed({"A.mkv": os.urandom(900_000)}, top="A")
    b = make_seed({"B.mkv": os.urandom(900_000)}, top="B")
    # A: opened, the player left after the first bytes; its source disappears so A stays incomplete.
    # Locally 900 KB would arrive in a blink (the opened file is fully wanted) and the pause below would
    # come too late, so A's seeder is throttled: the first piece takes ~1.5 s, the whole file ~45 s.
    a.session.get_torrents()[0].set_upload_limit(20_000)  # per torrent: applies to loopback peers too
    async with asyncio.timeout(T):
        await client.post("/create", json={"blob": a.torrent.hex()})
        connect_to_seed(torrent_session, a.info_hash, a)
        await (await client.get(f"/{a.info_hash}/0", headers={"Range": "bytes=0-1000"})).read()
    a.session.pause()
    ea = torrent_session.get(a.info_hash)
    await stop_watching(client, torrent_session, a.info_hash)
    assert ea.remaining_bytes() > 0

    # B is being watched and still downloading (its seeder is not connected yet) → A waits, upload only
    await client.post("/create", json={"blob": b.torrent.hex()})
    eb = torrent_session.get(b.info_hash)
    torrent_session.stream_opened(eb)
    eb.ensure_file_wanted(0)
    decisions = {d.ih: d for d in await torrent_session.library.tick()}
    assert decisions[a.info_hash].state.value == "waiting"
    assert "being watched and still downloading" in decisions[a.info_hash].reasons[0]
    assert ea.handle.flags() & lt.torrent_flags.upload_mode
    assert ea.handle.max_connections() == torrent_session.settings["bgMaxConnections"]

    # B's file is complete → A may finish in the background, even with B's player still open
    connect_to_seed(torrent_session, b.info_hash, b)
    await asyncio.wait_for(read_file(eb, 0, 10**7), T)
    decisions = {d.ih: d for d in await torrent_session.library.tick()}
    assert decisions[a.info_hash].state.value == "completing"
    assert not ea.handle.flags() & lt.torrent_flags.upload_mode
    await torrent_session.library.tick()  # nothing changed: no new lines
    torrent_session.stream_closed(eb)

    # every transition and every flip of the background gate is logged exactly once
    assert logged(caplog, "library")[-4:] == [
        "library: background downloads paused — 'B' is being watched and still downloading",
        "[A] library: completing → waiting (finishing opened files paused: 'B' is being watched and still downloading)",
        "library: background downloads allowed (nothing being watched needs the line)",
        "[A] library: waiting → completing (finishing opened files; nothing being watched needs the line)",
    ]


async def test_next_episode_is_prefetched_while_the_player_is_open(
    client, torrent_session, make_seed, caplog
):
    caplog.set_level(logging.INFO, logger="istok")
    torrent_session.settings.save({"prefetchHead": 64 * 1024, "prefetchTail": 32 * 1024})
    seed = make_seed(
        {
            "[G] Title - 01.mkv": os.urandom(500_000),
            "[G] Title - 02.mkv": os.urandom(500_000),
            "[G] Title - 03.mkv": os.urandom(500_000),
            "Subs/[G] Title - 02.srt": b"1\n00:00:01,000 --> 00:00:02,000\nhi\n",
        },
        top="T",
    )
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
    assert target.video_idx == ep2 and target.whole_files == (file_index(files, "[G] Title - 02.srt"),)

    deadline = time.monotonic() + T
    while any(not engine.handle.have_piece(p) for p in target.pieces) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    progress = engine.handle.file_progress()
    plen = engine.piece_length
    assert all(engine.handle.have_piece(p) for p in target.pieces)  # head and tail of episode 2
    assert 0 < progress[ep2] < files[ep2]["length"] - plen  # …but not the whole episode
    assert progress[file_index(files, "[G] Title - 03.mkv")] == 0  # episode 3 untouched
    await torrent_session.library.tick()
    await torrent_session.library.tick()
    torrent_session.stream_closed(engine)

    prefetch = logged(caplog, "prefetch")
    assert prefetch[0].startswith(
        '[T · [G] Title - 01.mkv] prefetch: next episode is "[G] Title - 02.mkv" — head 64.00 KB + tail 32.00 KB, '
        "1 subtitle(s)"
    )
    assert prefetch[-1].startswith("[T · [G] Title - 02.mkv] prefetch: done in ")
    assert sum("prefetch: started" in m for m in prefetch) == 1


async def test_cache_full_evicts_the_least_recently_watched(client, torrent_session, make_seed, caplog):
    caplog.set_level(logging.INFO, logger="istok")
    old = make_seed({"Old.mkv": os.urandom(700_000)}, top="Old")
    new = make_seed({"New.mkv": os.urandom(700_000)}, top="New")
    await watch(client, torrent_session, old, "Old.mkv")
    await stop_watching(client, torrent_session, old.info_hash)
    torrent_session.get(old.info_hash).last_watched = time.time() - 3600
    torrent_session.settings.save({"cacheSize": 1_000_000})  # room for one of them
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


async def test_background_upload_is_shared_while_watching(client, torrent_session, make_seed):
    torrent_session.settings.save({"streamBgUploadLimit": 200_000})
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
    from istok.session import TorrentSession

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
    assert {"cache", "background", "limits", "torrents", "recentlyEvicted"} <= set(report)
    assert report["cache"]["budget"] == torrent_session.settings["cacheSize"]
    (t,) = report["torrents"]
    assert t["state"] == "streaming" and t["sizeOnDisk"] > 0
