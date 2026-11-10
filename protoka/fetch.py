"""Downloading a .torrent from a URL the client gave (`/create {"from": …}`) without SSRF.

The server runs next to other services (a NAS, a router, cloud metadata at 169.254.169.254), and any web
page can reach it, so "fetch this URL" must not become a way to reach them. Only http(s) to public
addresses: IP literals are checked here, host names by the resolver (the connection uses exactly the
addresses that were checked, so DNS rebinding gets nothing), and redirects are followed by hand so every
hop goes through the same checks. Upstream read local files and fetched anything (B4).
"""

from __future__ import annotations

import ipaddress
import socket

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import DefaultResolver
from yarl import URL

MAX_REDIRECTS = 5
REDIRECTS = frozenset({301, 302, 303, 307, 308})


class FetchRefused(ValueError):
    """The URL (or a redirect) points somewhere the server must not go. The message is safe to return."""


def is_public_address(host: str) -> bool:
    """A globally routable unicast address (not loopback, private, link-local, CGNAT, reserved…)."""
    try:
        addr = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        mapped = addr.ipv4_mapped or addr.sixtofour
        if mapped is not None:
            addr = mapped
    return addr.is_global and not addr.is_multicast


def checked_url(url: str | URL) -> URL:
    """`url` if it may be fetched (its host name is checked again when it is resolved), else FetchRefused."""
    u = url if isinstance(url, URL) else URL(url)
    if u.scheme not in ("http", "https") or not u.host:
        raise FetchRefused("'from' must be an http(s) URL")
    try:
        ipaddress.ip_address(u.host.split("%", 1)[0])
    except ValueError:
        return u  # a name: PublicResolver decides
    if not is_public_address(u.host):
        raise FetchRefused("'from' must point to a public address")
    return u


class PublicResolver(AbstractResolver):
    """Resolves like aiohttp does, then keeps only public addresses (none left → the fetch fails)."""

    def __init__(self) -> None:
        self._inner = DefaultResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        found = await self._inner.resolve(host, port, family)
        public = [r for r in found if is_public_address(r["host"])]
        if not public:
            raise OSError(f"{host} has no public address")
        return public

    async def close(self) -> None:
        await self._inner.close()


async def fetch_torrent(url: str, limit: int, timeout: float = 30) -> bytes:
    """GET `url` (at most `limit` bytes). FetchRefused for a forbidden target, aiohttp.ClientError,
    TimeoutError or ValueError ("too large") otherwise."""
    target = checked_url(url)
    connector = aiohttp.TCPConnector(resolver=PublicResolver())
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=timeout)) as cs:
        for _ in range(MAX_REDIRECTS + 1):
            async with cs.get(target, allow_redirects=False) as resp:
                location = resp.headers.get("Location")
                if resp.status in REDIRECTS and location:
                    target = checked_url(target.join(URL(location)))
                    continue
                resp.raise_for_status()
                data = await resp.content.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("torrent file too large")
                return data
    raise FetchRefused("too many redirects")
