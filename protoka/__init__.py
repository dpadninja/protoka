"""Stremio-compatible streaming server (torrent core) built on libtorrent + aiohttp."""

from importlib.metadata import PackageNotFoundError, version

try:
    # single source of truth: [project].version in pyproject.toml (also used by scripts/publish.sh)
    __version__ = version("protoka")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.0.0+unknown"

# Version reported to clients as `serverVersion`; mirrors the upstream server we are compatible with.
COMPAT_SERVER_VERSION = "4.22.5"
