"""Origin whitelist (upstream server.js line 47763), with the matching bugs fixed (B4, B13).

Upstream used unanchored regexes with unescaped dots (".strem.io(:80)?$" matches "evilstrem.io",
"peario.xyz" matches anywhere) and the core router sent `Access-Control-Allow-Origin: *` regardless.
Here: one check for every route, comparing the parsed origin host.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

# host suffix rules: exact host or any subdomain
_DOMAIN_SUFFIXES = ("strem.io", "stremio.net", "stremio.com", "peario.xyz", "gstatic.com")
_EXACT_HOSTS = ("stremio.github.io", "stremio-development.netlify.app")
# netlify deploy previews: "deploy-preview-123--stremio-development.netlify.app"
_NETLIFY_PREVIEW_SUFFIX = "--stremio-development.netlify.app"
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _extra_origins() -> set[str]:
    raw = os.environ.get("CORS_ALLOW_ORIGINS", "")
    return {o.strip().rstrip("/").lower() for o in raw.split(",") if o.strip()}


def origin_allowed(origin: str | None, server_port: int | None = None) -> bool:
    if not origin:
        return False
    if os.environ.get("NO_CORS"):
        return True
    norm = origin.strip().rstrip("/").lower()
    if norm in _extra_origins():
        return True
    try:
        parts = urlsplit(norm)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not host:
        return False
    if host in _LOOPBACK:
        # upstream: "(127.0.0.1|localhost):11470$" — the server's own web UI
        return server_port is not None and port == server_port
    if host in _EXACT_HOSTS or host.endswith(_NETLIFY_PREVIEW_SUFFIX):
        return True
    return any(host == d or host.endswith("." + d) for d in _DOMAIN_SUFFIXES)
