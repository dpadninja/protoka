"""Downloading a .torrent from a URL the client gave (`/create {"from": …}`) without SSRF.

The server runs next to other services (a NAS, a router, cloud metadata at 169.254.169.254), and any web
page can reach it, so "fetch this URL" must not become a way to reach them. Only http(s) to public
addresses, plus the networks the admin listed in FETCH_ALLOW_NETWORKS (a LAN indexer): IP literals are
checked here, host names by the resolver (the connection uses exactly the addresses that were checked, so
DNS rebinding gets nothing), and redirects are followed by hand so every hop goes through the same checks.
Upstream read local files and fetched anything (B4).
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import DefaultResolver
from yarl import URL

MAX_REDIRECTS = 5
REDIRECTS = frozenset({301, 302, 303, 307, 308})

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class FetchRefused(ValueError):
    """The URL (or a redirect) points somewhere the server must not go. The message is safe to return."""


def networks(items: Iterable[str]) -> tuple[Network, ...]:
    """Parsed FETCH_ALLOW_NETWORKS entries (already validated by config)."""
    return tuple(ipaddress.ip_network(n, strict=False) for n in items)


def _address(host: str) -> IPAddress | None:
    try:
        addr = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address):
        embedded = addr.ipv4_mapped or addr.sixtofour  # an IPv4 address in IPv6 clothing
        if embedded is not None:
            return embedded
    return addr


def is_public_address(host: str) -> bool:
    """A globally routable unicast address (not loopback, private, link-local, CGNAT, reserved…)."""
    addr = _address(host)
    return addr is not None and addr.is_global and not addr.is_multicast


def is_allowed_address(host: str, allow: Iterable[Network] = ()) -> bool:
    """Public, or inside one of the networks the admin allowed."""
    if is_public_address(host):
        return True
    addr = _address(host)
    return addr is not None and any(addr.version == n.version and addr in n for n in allow)


def checked_url(url: str | URL, allow: Iterable[Network] = ()) -> URL:
    """`url` if it may be fetched (its host name is checked again when it is resolved), else FetchRefused."""
    u = url if isinstance(url, URL) else URL(url)
    if u.scheme not in ("http", "https") or not u.host:
        raise FetchRefused("'from' must be an http(s) URL")
    if _address(u.host) is None:
        return u  # a name: PublicResolver decides
    if not is_allowed_address(u.host, allow):
        raise FetchRefused("'from' must point to a public address (or one in FETCH_ALLOW_NETWORKS)")
    return u


class PublicResolver(AbstractResolver):
    """Resolves like aiohttp does, then keeps only allowed addresses (none left → the fetch fails)."""

    def __init__(self, allow: Iterable[Network] = ()) -> None:
        self._inner = DefaultResolver()
        self._allow = tuple(allow)

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        found = await self._inner.resolve(host, port, family)
        ok = [r for r in found if is_allowed_address(r["host"], self._allow)]
        if not ok:
            raise OSError(f"{host} has no public address")
        return ok

    async def close(self) -> None:
        await self._inner.close()


async def fetch_torrent(url: str, limit: int, allow: Iterable[str] = (), timeout: float = 30) -> bytes:
    """GET `url` (at most `limit` bytes); `allow`: extra networks (CIDR strings). FetchRefused for a
    forbidden target, aiohttp.ClientError, TimeoutError or ValueError ("too large") otherwise."""
    nets = networks(allow)
    target = checked_url(url, nets)
    connector = aiohttp.TCPConnector(resolver=PublicResolver(nets))
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=timeout)) as cs:
        for _ in range(MAX_REDIRECTS + 1):
            async with cs.get(target, allow_redirects=False) as resp:
                location = resp.headers.get("Location")
                if resp.status in REDIRECTS and location:
                    target = checked_url(target.join(URL(location)), nets)
                    continue
                resp.raise_for_status()
                data = await resp.content.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("torrent file too large")
                return data
    raise FetchRefused("too many redirects")
