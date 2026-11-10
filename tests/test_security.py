"""Hardening against untrusted input (the CodeQL findings): SSRF, paths, ReDoS, error and log contents."""

from __future__ import annotations

import time

import pytest
from aiohttp import web

from protoka import logfmt
from protoka.fetch import FetchRefused, PublicResolver, checked_url, is_public_address
from protoka.stream import ByteRange, parse_range

# -- SSRF: /create {"from": …} ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "host,public",
    [
        ("8.8.8.8", True),
        ("2606:4700:4700::1111", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("192.168.1.10", False),
        ("172.16.0.1", False),
        ("100.64.0.1", False),  # CGNAT
        ("169.254.169.254", False),  # cloud metadata
        ("0.0.0.0", False),  # noqa: S104 — an address under test, not a bind
        ("224.0.0.1", False),
        ("::1", False),
        ("fe80::1%eth0", False),
        ("fd00::1", False),
        ("::ffff:127.0.0.1", False),  # IPv4-mapped loopback
        ("::ffff:8.8.8.8", True),
        ("2002:7f00:1::", False),  # 6to4 around 127.0.0.1
        ("example.com", False),  # a name is not an address
    ],
)
def test_is_public_address(host, public):
    assert is_public_address(host) is public


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "/etc/passwd",
        "ftp://example.com/x.torrent",
        "http://127.0.0.1:11470/x.torrent",
        "http://[::1]/x.torrent",
        "http://[::ffff:127.0.0.1]/x.torrent",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.1/",
    ],
)
def test_checked_url_refuses(url):
    with pytest.raises(FetchRefused):
        checked_url(url)


@pytest.mark.parametrize("url", ["https://example.com/a.torrent", "http://8.8.8.8/a.torrent"])
def test_checked_url_allows(url):
    assert str(checked_url(url)) == url


@pytest.mark.parametrize("host", ["localhost", "2130706433", "0x7f.1"])  # names for 127.0.0.1
async def test_resolver_drops_private_addresses(host):
    resolver = PublicResolver()
    try:
        with pytest.raises(OSError):
            await resolver.resolve(host, 80)
    finally:
        await resolver.close()


@pytest.fixture
async def torrent_server(aiohttp_server):
    """A local HTTP server that would hand out a .torrent; records whether it was ever asked."""
    hits: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        hits.append(request.path)
        return web.Response(body=b"d4:infod4:name1:xee")

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    server = await aiohttp_server(app)
    return server, hits


async def test_create_from_private_ip_is_refused(client, torrent_server):
    server, hits = torrent_server
    resp = await client.post("/create", json={"from": str(server.make_url("/x.torrent"))})
    assert resp.status == 400
    assert hits == []


async def test_create_from_name_of_private_host_is_refused(client, torrent_server):
    server, hits = torrent_server
    resp = await client.post("/create", json={"from": f"http://localhost:{server.port}/x.torrent"})
    assert resp.status >= 400
    assert hits == []
    assert "localhost" not in (await resp.json())["error"]


# -- paths: the info hash names a cache directory ---------------------------------------------------


@pytest.mark.parametrize("ih", ["..", "../../etc", "a" * 39, "g" * 40, "/" + "a" * 39, "a" * 40 + "/.."])
def test_cache_path_requires_an_info_hash(torrent_session, ih):
    with pytest.raises(ValueError):
        torrent_session.cache_path(ih)


def test_cache_path_is_inside_the_cache(torrent_session):
    p = torrent_session.cache_path("A" * 40)
    assert p.parent == torrent_session.settings.cache_dir()
    assert p.name == "a" * 40


async def test_remove_and_purge_ignore_bad_hashes(torrent_session, tmp_path):
    victim = torrent_session.settings.cache_dir().parent / "keep"
    victim.mkdir(parents=True, exist_ok=True)
    assert await torrent_session.remove("../keep", purge=True) is False
    assert await torrent_session.purge("../keep") is False
    assert victim.exists()


async def test_create_rejects_bad_hash(torrent_session):
    with pytest.raises(ValueError):
        await torrent_session.create("../" + "a" * 37)


# -- ReDoS: Range header -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header,expected",
    [
        (" bytes = 10 - 20 ", ByteRange(10, 20)),
        ("BYTES=10-", ByteRange(10, 999)),
        ("bytes=1-2-3", None),
        ("bytes=--5", None),
        ("bytes=٣-٤", None),  # non-ASCII digits
        ("bytes 0-1", None),
    ],
)
def test_range_parsing_edge_cases(header, expected):
    assert parse_range(header, 1000) == expected


def test_range_parsing_is_linear():
    started = time.monotonic()
    for header in ("bytes=-" + " " * 100_000 + "x", "bytes=" + " " * 100_000 + "-" + " " * 100_000 + "x"):
        assert parse_range(header, 1000) is None
    assert time.monotonic() - started < 0.5


# -- error responses and log lines ---------------------------------------------------------------------


async def test_invalid_body_does_not_echo_parser_error(client):
    resp = await client.post("/create", data=b'{"blob": "\x01', headers={"Content-Type": "application/json"})
    assert resp.status == 400
    assert await resp.json() == {"error": "invalid body"}


async def test_create_failure_is_generic(client):
    resp = await client.post("/create", json={"blob": "00"})
    assert resp.status == 500
    assert await resp.json() == {"error": "cannot load the torrent"}


def test_log_clean_escapes_control_characters():
    assert logfmt.clean("a\nb\r\x1b[31m\x00") == "a\\nb\\r\\x1b[31m\\x00"
    assert "\n" not in logfmt.clean("x\n" * 500)
    assert len(logfmt.clean("y" * 1000)) == 201
