"""End-to-end API tests against a local libtorrent seeder (fully offline)."""

from __future__ import annotations

import asyncio

import pytest

from istok.session import TorrentSession
from tests.conftest import Seed, connect_to_seed, file_index

T = 30  # seconds per network-ish step


async def create_via_hash(client, ses: TorrentSession, seed: Seed, body: dict | None = None):
    """Magnet-style creation: engine without metadata, connect the seeder, then POST /:ih/create."""
    await ses.create(seed.info_hash)
    connect_to_seed(ses, seed.info_hash, seed)
    async with asyncio.timeout(T):
        resp = await client.post(f"/{seed.info_hash}/create", json=body or {})
    assert resp.status == 200
    return await resp.json()


async def test_heartbeat(client):
    resp = await client.get("/heartbeat")
    assert resp.status == 200
    assert await resp.json() == {"success": True}


async def test_create_from_blob(client, seed: Seed, torrent_session):
    resp = await client.post("/create", json={"blob": seed.torrent.hex()})
    assert resp.status == 200
    stats = await resp.json()
    assert stats["infoHash"] == seed.info_hash
    names = [f["name"] for f in stats["files"]]
    assert {"Show.S01E01.mkv", "Show.S01E02.mkv", "sample.txt"} <= set(names)
    assert stats["files"][0]["path"].startswith("Show/")
    for key in (
        "peers",
        "unchoked",
        "queued",
        "unique",
        "connectionTries",
        "swarmPaused",
        "swarmConnections",
        "swarmSize",
        "selections",
        "wires",
        "downloaded",
        "uploaded",
        "downloadSpeed",
        "uploadSpeed",
        "sources",
        "peerSearchRunning",
        "opts",
    ):
        assert key in stats, key
    assert any(s["url"].startswith("dht:") for s in stats["sources"])


async def test_create_from_local_path_is_rejected(client, tmp_path):  # B4
    p = tmp_path / "x.torrent"
    p.write_bytes(b"d4:infod4:name1:xee")
    resp = await client.post("/create", json={"from": str(p)})
    assert resp.status == 400


async def test_create_bad_blob(client):
    resp = await client.post("/create", json={"blob": "zz"})
    assert resp.status == 500
    assert "error" in await resp.json()


async def test_create_by_hash_fetches_metadata_and_guesses(client, seed: Seed, torrent_session):
    stats = await create_via_hash(
        client, torrent_session, seed, {"guessFileIdx": {"season": 1, "episode": 2}}
    )
    assert stats["guessedFileIdx"] == file_index(stats["files"], "Show.S01E02.mkv")
    assert (torrent_session.cache_path(seed.info_hash) / "metadata.torrent").exists()


async def test_create_by_hash_guess_empty_object(client, seed: Seed, torrent_session):
    stats = await create_via_hash(client, torrent_session, seed, {"guessFileIdx": {}})  # {} is truthy in JS
    assert stats["guessedFileIdx"] == file_index(stats["files"], "Show.S01E02.mkv")


async def test_create_by_hash_file_must_include(client, seed: Seed, torrent_session):
    stats = await create_via_hash(client, torrent_session, seed, {"fileMustInclude": ["/s01e01/i"]})
    assert stats["guessedFileIdx"] == file_index(stats["files"], "Show.S01E01.mkv")


async def _stream_ready(client, ses, seed) -> list[dict]:
    stats = await create_via_hash(client, ses, seed)
    return stats["files"]


async def test_stream_full_file(client, seed: Seed, torrent_session):
    files = await _stream_ready(client, torrent_session, seed)
    idx = file_index(files, "Show.S01E02.mkv")
    async with asyncio.timeout(T):
        resp = await client.get(f"/{seed.info_hash}/{idx}")
        body = await resp.read()
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "video/x-matroska"
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert resp.headers["transferMode.dlna.org"] == "Streaming"
    assert int(resp.headers["Content-Length"]) == len(seed.content("Show.S01E02.mkv"))
    assert body == seed.content("Show.S01E02.mkv")

    async with asyncio.timeout(T):
        st = await (await client.get(f"/{seed.info_hash}/{idx}/stats.json")).json()
    assert st["streamProgress"] == 1
    assert st["streamName"] == "Show.S01E02.mkv"
    assert st["streamLen"] == len(body)
    assert st["wires"] is None


@pytest.mark.parametrize(
    "rng,slc",
    [
        ("bytes=1000-99999", slice(1000, 100000)),
        ("bytes=-5000", slice(-5000, None)),
        ("bytes=32760-32780", slice(32760, 32781)),  # crosses a piece boundary
        ("bytes=450000-", slice(450000, None)),
    ],
)
async def test_stream_ranges(client, seed: Seed, torrent_session, rng, slc):
    files = await _stream_ready(client, torrent_session, seed)
    idx = file_index(files, "Show.S01E02.mkv")
    content = seed.content("Show.S01E02.mkv")
    async with asyncio.timeout(T):
        resp = await client.get(f"/{seed.info_hash}/{idx}", headers={"Range": rng})
        body = await resp.read()
    expected = content[slc]
    assert resp.status == 206
    assert body == expected
    start = slc.start if slc.start >= 0 else len(content) + slc.start
    assert resp.headers["Content-Range"] == f"bytes {start}-{start + len(expected) - 1}/{len(content)}"


async def test_stream_unsatisfiable_range(client, seed: Seed, torrent_session):
    files = await _stream_ready(client, torrent_session, seed)
    idx = file_index(files, "sample.txt")
    resp = await client.get(f"/{seed.info_hash}/{idx}", headers={"Range": "bytes=999999-"})
    assert resp.status == 416
    assert resp.headers["Content-Range"] == f"bytes */{len(seed.content('sample.txt'))}"


async def test_head(client, seed: Seed, torrent_session):
    files = await _stream_ready(client, torrent_session, seed)
    idx = file_index(files, "Show.S01E01.mkv")
    resp = await client.head(f"/{seed.info_hash}/{idx}", headers={"Range": "bytes=0-99"})
    assert resp.status == 206
    assert resp.headers["Content-Length"] == "100"
    assert await resp.read() == b""


async def test_stream_guess_and_by_name(client, seed: Seed, torrent_session):
    await _stream_ready(client, torrent_session, seed)
    async with asyncio.timeout(T):
        resp = await client.get(f"/{seed.info_hash}/-1", headers={"Range": "bytes=0-9"})
        assert await resp.read() == seed.content("Show.S01E02.mkv")[:10]  # largest media file
        resp = await client.get(f"/{seed.info_hash}/sample.txt")
        assert await resp.read() == seed.content("sample.txt")
        resp = await client.get(f"/{seed.info_hash}/0/any/trailing/name.mkv", headers={"Range": "bytes=0-0"})
        assert resp.status == 206


async def test_stream_file_must_include_query(client, seed: Seed, torrent_session):
    await _stream_ready(client, torrent_session, seed)
    async with asyncio.timeout(T):
        resp = await client.get(f"/{seed.info_hash}/-1?f=sample", headers={"Range": "bytes=0-4"})
    assert await resp.read() == b"hello"


async def test_stream_bad_index(client, seed: Seed, torrent_session):
    await _stream_ready(client, torrent_session, seed)
    for idx in ("99", "1e1", "nope.mkv"):
        resp = await client.get(f"/{seed.info_hash}/{idx}")
        assert resp.status == 404, idx
        assert "error" in await resp.json()


async def test_external_redirect(client, seed: Seed, torrent_session):
    files = await _stream_ready(client, torrent_session, seed)
    idx = file_index(files, "Show.S01E01.mkv")
    resp = await client.get(f"/{seed.info_hash}/{idx}?external=1&download=1", allow_redirects=False)
    assert resp.status == 307
    assert resp.headers["Location"] == f"/{seed.info_hash}/Show.S01E01.mkv?download=1"


async def test_stats_remove_and_purge(client, seed: Seed, torrent_session):
    await _stream_ready(client, torrent_session, seed)
    all_stats = await (await client.get("/stats.json?sys=1")).json()
    assert seed.info_hash in all_stats and "loadavg" in all_stats["sys"]
    upper = await (await client.get(f"/{seed.info_hash.upper()}/stats.json")).json()  # B3
    assert upper["infoHash"] == seed.info_hash

    # /remove (Stremio calls it when playback ends) only ends "watching": the torrent keeps seeding
    assert await (await client.get(f"/{seed.info_hash}/remove")).json() == {}
    await torrent_session.library.tick()
    assert torrent_session.get(seed.info_hash).state != "streaming"
    assert seed.info_hash not in await (await client.get("/stats.json")).json()  # only what is watched
    cache = torrent_session.cache_path(seed.info_hash)
    assert cache.exists()

    # ?purge=1 deletes it for real
    assert await (await client.get(f"/{seed.info_hash}/remove?purge=1")).json() == {}
    assert await (await client.get(f"/{seed.info_hash}/stats.json")).json() is None
    assert not cache.exists()


async def test_remove_all_keeps_seeding_and_purge_waits(client, seed: Seed, torrent_session):
    await _stream_ready(client, torrent_session, seed)
    assert await (await client.get("/removeAll")).json() == {}
    assert seed.info_hash in torrent_session.engines  # still seeding
    assert await (await client.get("/removeAll?purge=1")).json() == {}
    assert torrent_session.engines == {}  # answered after the removal finished (B5)


async def test_reopen_after_restart_without_peers(client, seed: Seed, torrent_session, settings):
    files = await _stream_ready(client, torrent_session, seed)
    idx = file_index(files, "sample.txt")
    async with asyncio.timeout(T):
        assert await (await client.get(f"/{seed.info_hash}/{idx}")).read() == seed.content("sample.txt")
    seed.session.pause()  # no peers any more: after a restart metadata + data must come from the cache
    await torrent_session.close()
    from tests.conftest import OFFLINE

    restarted = TorrentSession(settings, overrides=OFFLINE, default_trackers=False)
    await restarted.start()
    try:
        assert seed.info_hash in restarted.engines  # restored from the cache, will seed again
        engine = restarted.get(seed.info_hash)
        await asyncio.wait_for(engine.wait_metadata(), T)
        from istok.stream import read_file

        restarted.stream_opened(engine)
        data = await asyncio.wait_for(read_file(engine, idx, 10**6), T)
        assert data == seed.content("sample.txt")
    finally:
        await restarted.close()


async def test_torrent_becomes_a_seed_after_the_grace_period(client, seed: Seed, torrent_session):
    torrent_session.settings.save({"streamGrace": 0.2})
    await _stream_ready(client, torrent_session, seed)
    await asyncio.sleep(0.5)
    await torrent_session.library.tick()
    engine = torrent_session.get(seed.info_hash)
    assert engine is not None and engine.state != "streaming"  # kept, not unloaded


async def test_cors(client, monkeypatch):
    monkeypatch.delenv("NO_CORS", raising=False)
    resp = await client.get("/heartbeat", headers={"Origin": "https://web.stremio.com"})
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"
    resp = await client.get("/heartbeat", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in resp.headers
    resp = await client.options(
        "/stats.json",
        headers={"Origin": "https://web.stremio.com", "Access-Control-Request-Headers": "range,content-type"},
    )
    assert resp.status == 200
    assert resp.headers["Access-Control-Allow-Headers"] == "range,content-type"
    assert resp.headers["Access-Control-Allow-Methods"] == "POST, GET, OPTIONS"


async def test_settings_roundtrip(client, app_path):
    data = await (await client.get("/settings")).json()
    assert {"options", "values", "baseUrl"} <= set(data)
    assert data["values"]["btMaxConnections"] == 200
    resp = await client.post("/settings", json={"btMaxConnections": 80, "cacheSize": None})
    assert await resp.json() == {"success": True}
    values = (await (await client.get("/settings")).json())["values"]
    assert values["btMaxConnections"] == 80 and values["cacheSize"] is None

    resp = await client.post("/settings", json={"cacheRoot": "relative/path"})
    assert resp.status == 400
    assert "error" in await resp.json()


async def test_misc_routes(client):
    assert (await client.get("/favicon.ico")).status == 404
    ni = await (await client.get("/network-info")).json()
    assert isinstance(ni["availableInterfaces"], list)
    resp = await client.get("/", allow_redirects=False)
    assert resp.status == 307
    assert "streamingServer=http%3A%2F%2F" in resp.headers["Location"]
    unknown = "0" * 40
    assert await (await client.get(f"/{unknown}/stats.json")).json() is None


async def test_new_sources_are_added_to_an_existing_engine(client, seed: Seed, torrent_session):
    """Regression: handle.trackers() returns dicts; adding trackers to a live engine used to crash."""
    await _stream_ready(client, torrent_session, seed)
    body = {
        "peerSearch": {
            "sources": ["tracker:udp://new.tracker.example:1337/announce", f"dht:{seed.info_hash}"]
        }
    }
    async with asyncio.timeout(T):
        resp = await client.post(f"/{seed.info_hash}/create", json=body)
    assert resp.status == 200
    urls = [t["url"] for t in torrent_session.get(seed.info_hash).handle.trackers()]
    assert "udp://new.tracker.example:1337/announce" in urls
    stats = await resp.json()
    assert any(s["url"] == "tracker:udp://new.tracker.example:1337/announce" for s in stats["sources"])


async def test_stats_satisfy_the_stremio_core_contract(client, seed: Seed, torrent_session):
    """Stremio drops the whole stats response if one field breaks its serde schema (no peers/speed shown)."""
    from tests.stremio_contract import validate

    files = await _stream_ready(client, torrent_session, seed)
    idx = file_index(files, "Show.S01E02.mkv")
    async with asyncio.timeout(T):
        await (await client.get(f"/{seed.info_hash}/{idx}", headers={"Range": "bytes=0-1000"})).read()
        engine_level = await (await client.get(f"/{seed.info_hash}/stats.json")).json()
        file_level = await (await client.get(f"/{seed.info_hash}/{idx}/stats.json")).json()
        created = await (await client.post(f"/{seed.info_hash}/create", json={"guessFileIdx": {}})).json()
    for name, stats in (("engine", engine_level), ("file", file_level), ("create", created)):
        assert validate(stats) == [], name
    assert file_level["peers"] >= 1 and file_level["streamName"] == "Show.S01E02.mkv"


def test_contract_validator_catches_what_broke_stremio():
    """The two real defects: a null `lastStarted` and missing `opts.growler`/`swarmCap`/`virtual`."""
    from tests.stremio_contract import validate

    bad = {"sources": [{"lastStarted": None, "numFound": 0, "numFoundUniq": 0, "numRequests": 0, "url": "dht:x"}],
           "opts": {"dht": True, "tracker": True, "path": "/x", "peerSearch": {"min": 1, "max": 2, "sources": []}}}  # fmt: skip
    errors = validate(bad)
    assert "stats.sources[0].lastStarted: null, expected String" in errors
    assert {
        "stats.opts.growler: missing",
        "stats.opts.swarmCap: missing",
        "stats.opts.virtual: missing",
    } <= set(errors)


async def test_client_disconnect_mid_stream_is_not_an_error(caplog):
    """Players abort Range requests on every seek: writing then fails with ClientConnectionResetError.
    That must end the request quietly (no traceback), still releasing the stream and its pieces."""
    import logging
    from types import SimpleNamespace

    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from istok.http_app import SESSION_KEY, _serve_stream

    events: list[str] = []

    class ClosingResponse(web.StreamResponse):
        async def prepare(self, request):
            return None

        async def write(self, data):
            raise aiohttp.ClientConnectionResetError("Cannot write to closing transport")

    async def chunks():
        try:
            yield b"x" * 10
        finally:
            events.append("chunks closed")  # deadlines released by FileStream.close()

    app = web.Application()
    app[SESSION_KEY] = SimpleNamespace(  # type: ignore[misc]
        stream_opened=lambda e: events.append("opened"), stream_closed=lambda e: events.append("closed")
    )
    request = make_mocked_request("GET", "/x", app=app)
    engine = SimpleNamespace(info_hash="ab" * 20, ensure_file_wanted=lambda idx: None)
    with caplog.at_level(logging.DEBUG):
        resp = await _serve_stream(request, engine, 0, {}, chunks(), resp=ClosingResponse())
    assert isinstance(resp, web.StreamResponse)
    assert events == ["opened", "chunks closed", "closed"]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
