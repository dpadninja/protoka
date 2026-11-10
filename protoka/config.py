"""Server settings: the defaults plus the environment, fixed for the life of the process.

Keys and `/settings` shape follow upstream (analysis/server-v4.22.5.js ~line 12979 settings module, 36435
getOptions), but nothing is persisted and Stremio cannot change them: the server is configured where it runs.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from protoka import COMPAT_SERVER_VERSION

log = logging.getLogger(__name__)

# Upstream defaults, except btMaxConnections (55 upstream; raised — see analysis/02-peer-discovery.md P6).
DEFAULTS: dict[str, Any] = {
    "cacheSize": 128 * 1024**3,  # 128 GB (upstream: 2 GB); everything in the cache keeps seeding
    "btMaxConnections": 200,
    "btHandshakeTimeout": 20000,
    "btRequestTimeout": 4000,
    "btDownloadSpeedSoftLimit": 2621440,
    "btDownloadSpeedHardLimit": 3670016,
    "btMinPeersForStable": 5,
    "remoteHttps": "",
    "localAddonEnabled": False,
    "transcodeHorsepower": 0.75,
    "transcodeMaxBitRate": 0,
    "transcodeConcurrency": 1,
    "transcodeTrackConcurrency": 1,
    "transcodeHardwareAccel": True,
    "transcodeProfile": None,
    "allTranscodeProfiles": [],
    "transcodeMaxWidth": 1920,
    "proxyStreamsEnabled": False,
    # New in this implementation: BitTorrent listen port (TCP+uTP). Upstream never listened (P1).
    "btListenPort": 42069,
    # New: per-torrent download rate limit in bytes/s, 0 = unlimited (replaces the soft/hard limits).
    "btDownloadLimit": 0,
    # New: embed sidecar subtitles + fonts found in the torrent into the video stream.
    "subtitlesMux": True,
    # New: replace fonts a script needs but the torrent lacks by bundled ones with a similar letterform
    # (Noto, Noto CJK, Liberation; analysis/17-font-fallback.md), searched in this directory.
    "fontFallback": True,
    "fallbackFontsDir": "/usr/share/fonts",
    # New: external audio. ISO 639-2 language whose sidecar track becomes the default ("" = keep the
    # video's default), and how long to wait for a raw stream (.ac3, .dts…) to be repacked, seconds.
    "preferredAudioLang": "",
    "muxAudioWait": 90,
    "muxRepackConcurrency": 1,
    # Seeding (analysis/15-seeding.md). 0 = no limit.
    "seedUploadLimit": 0,  # global upload cap, bytes/s
    "streamBgUploadLimit": 0,  # while watching: total upload of the *other* torrents, bytes/s
    "seedMaxActive": 0,  # libtorrent active_seeds
    "seedRatioLimit": 0.0,
    "seedTimeLimit": 0.0,  # seconds
    "bgMaxConnections": 20,  # per torrent that is not being watched
    "streamGrace": 120.0,  # seconds a torrent counts as "being watched" after the last activity
    "prefetchNext": True,
    "minFreeDisk": 2 * 1024**3,
    # CACHE_SIZE never evicts a torrent watched within this many seconds (0 = off); low disk space still does
    "cacheKeepRecent": 24 * 3600.0,
    # New: `/create {"from": url}` fetches only from public addresses; these networks (CIDR) are allowed too
    # (a LAN indexer such as Jackett). Empty = public only.
    "fetchAllowNetworks": [],
}

POSITIVE_INT_KEYS = (
    "btMaxConnections",
    "btHandshakeTimeout",
    "btRequestTimeout",
    "btDownloadSpeedSoftLimit",
    "btDownloadSpeedHardLimit",
    "btMinPeersForStable",
    "transcodeConcurrency",
    "transcodeTrackConcurrency",
)


def is_positive_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def default_app_path() -> Path:
    env = os.environ.get("APP_PATH")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".stremio-server"  # the upstream server's location: Stremio and old installs use it


class SettingsError(ValueError):
    status = 400


class Settings:
    """Dict-like settings: `DEFAULTS` + `update()` (the environment at startup, tests). Never saved.

    `values()` is what `GET /settings` returns as `values`."""

    def __init__(self, app_path: Path):
        self.app_path = Path(app_path)
        self._data = self._normalize({})

    def _normalize(self, data: dict[str, Any]) -> dict[str, Any]:
        out = dict(DEFAULTS)
        out.update(data)
        for k in POSITIVE_INT_KEYS:
            if not is_positive_int(out.get(k)):
                out[k] = DEFAULTS[k]
        # Upstream encodes "∞" as Infinity which JSON turns into null (B16): null == unlimited.
        # No "no caching" mode (upstream's 0: data in memory, dropped on close).
        cs = out.get("cacheSize")
        if cs is not None and not (isinstance(cs, (int, float)) and not isinstance(cs, bool) and cs > 0):
            out["cacheSize"] = DEFAULTS["cacheSize"]
        if not isinstance(out.get("cacheRoot"), str) or not out["cacheRoot"]:
            out["cacheRoot"] = str(self.app_path)
        if not isinstance(out.get("remoteHttps"), str):
            out["remoteHttps"] = ""
        nets = out.get("fetchAllowNetworks")
        out["fetchAllowNetworks"] = [str(n) for n in nets] if isinstance(nets, list) else []
        lim = out.get("btDownloadLimit")
        if not (isinstance(lim, int) and not isinstance(lim, bool) and lim >= 0):
            out["btDownloadLimit"] = 0
        port = out.get("btListenPort")
        if not (isinstance(port, int) and not isinstance(port, bool) and 0 <= port <= 65535):
            out["btListenPort"] = DEFAULTS["btListenPort"]
        out["appPath"] = str(self.app_path)
        out["serverVersion"] = COMPAT_SERVER_VERSION
        return out

    def update(self, changes: dict[str, Any]) -> None:
        new = self._normalize({**self._data, **changes})
        if "cacheRoot" in changes:
            new["cacheRoot"] = validate_cache_root(new["cacheRoot"])
        self._data = new

    # -- access ------------------------------------------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def values(self) -> dict[str, Any]:
        return dict(self._data)

    @property
    def cache_size(self) -> float:
        """Bytes; inf when unlimited."""
        cs = self._data.get("cacheSize")
        return float("inf") if cs is None else float(cs)

    def cache_dir(self) -> Path:
        """`<cacheRoot>/stremio-cache`, falling back to the temp dir like upstream."""
        root = Path(self._data["cacheRoot"])
        cache = root / "stremio-cache"
        try:
            cache.mkdir(parents=True, exist_ok=True)
            if cache.is_symlink():
                raise OSError("Cache directory is a symbolic link")
            return cache
        except OSError as e:
            log.error("Cannot use cache location %s: %s", cache, e)
            fallback = Path(tempfile.gettempdir()) / "stremio-cache"
            fallback.mkdir(parents=True, exist_ok=True)
            if fallback.is_symlink():
                raise OSError("Fallback cache directory is a symbolic link") from e
            return fallback


def validate_cache_root(root: Any) -> str:
    if not isinstance(root, str) or not root:
        raise SettingsError("cacheRoot must be a non-empty string")
    p = Path(root).expanduser()
    if not p.is_absolute():
        raise SettingsError("cacheRoot must be an absolute path")
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        if e.errno == 36:  # ENAMETOOLONG
            raise SettingsError("The cache folder path is too long. Choose a shorter folder.") from e
        raise SettingsError(f"Cannot use cache folder: {e.strerror or e}") from e
    if not p.is_dir():
        raise SettingsError("cacheRoot is not a directory")
    return str(p)


def settings_options(settings: Settings, ipv4_addresses: list[str]) -> list[dict[str, Any]]:
    """`options` block of GET /settings (upstream getOptions, linux branch)."""
    return [
        {"id": "localAddonEnabled", "label": "ENABLE_LOCAL_FILES_ADDON", "type": "checkbox"},
        {
            "id": "remoteHttps",
            "label": "ENABLE_REMOTE_HTTPS_CONN",
            "type": "select",
            "class": "https",
            "icon": True,
            "selections": [{"name": "Disabled", "val": ""}] + [{"name": a, "val": a} for a in ipv4_addresses],
        },
        {
            "id": "cacheSize",
            "label": "CACHING",
            "type": "select",
            "class": "caching",
            "icon": True,
            "selections": [
                {"name": "2GB", "val": 2147483648},
                {"name": "5GB", "val": 5368709120},
                {"name": "10GB", "val": 10737418240},
                {"name": "50GB", "val": 50 * 1024**3},
                {"name": "100GB", "val": 100 * 1024**3},
                {"name": "128GB", "val": 128 * 1024**3},
                {"name": "200GB", "val": 200 * 1024**3},
                {"name": "∞", "val": None},
            ],
        },
        {
            "id": "cacheRoot",
            "label": "SETTINGS_CACHING_DRIVE",
            "type": "directory",
            "class": "caching",
            "supportsDirectory": True,
            "selections": [{"name": "Default", "val": str(settings.app_path)}],
        },
    ]


# -- environment overrides (container deployments: TrueNAS Custom App, docker run -e …) -------------
_SIZE_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


class EnvError(ValueError):
    """A configuration environment variable has an invalid value (reported at startup)."""


def parse_size(value: str) -> int | None:
    """`"0"` → 0, `"50G"`/`"512m"`/`"1073741824"` → bytes, `"unlimited"`/`"inf"` → None (no limit)."""
    v = value.strip().lower().removesuffix("b").removesuffix("i")
    if v in ("unlimited", "inf", "infinity", "none", "-1"):
        return None
    num, unit = (v[:-1], v[-1]) if v and v[-1] in _SIZE_UNITS else (v, "")
    try:
        n = float(num)
    except ValueError as e:
        raise EnvError(f"invalid size {value!r} (examples: 128G, 512M, unlimited)") from e
    if n < 0:
        raise EnvError(f"size must not be negative: {value!r}")
    return int(n * _SIZE_UNITS[unit])


def env_overrides(environ: dict[str, str] | os._Environ[str]) -> dict[str, Any]:
    """Settings from environment variables: the only way to configure the server (see README).

    BT_PORT     BitTorrent listen port (must match the port forwarded/published for the container)
    PREFERRED_AUDIO_LANG  sidecar audio in this language becomes the default track (ru, rus, russian)
    MUX_AUDIO_WAIT        seconds to wait for a raw audio stream to be repacked (0 = do not wait)
    FONT_FALLBACK         replace fonts missing from a torrent by bundled ones (on by default)
    FALLBACK_FONTS_DIR    where the bundled fonts are (/usr/share/fonts)
    CACHE_ROOT  directory of the torrent cache (e.g. a separate large dataset mounted at /cache)
    CACHE_SIZE  cache size limit: bytes, 128G, 512M or unlimited (there is no "no caching" mode)
    FETCH_ALLOW_NETWORKS  non-public networks `/create {"from": url}` may fetch from (192.168.1.0/24)
    """
    out: dict[str, Any] = {}
    if raw := environ.get("BT_PORT", "").strip():
        if not raw.isdigit() or not 0 <= int(raw) <= 65535:
            raise EnvError(f"BT_PORT must be a port number 0-65535, got {raw!r}")
        out["btListenPort"] = int(raw)
    if raw := environ.get("CACHE_ROOT", "").strip():
        if not os.path.isabs(raw):
            raise EnvError(f"CACHE_ROOT must be an absolute path, got {raw!r}")
        out["cacheRoot"] = raw
    if raw := environ.get("CACHE_SIZE", "").strip():
        size = parse_size(raw)
        if size == 0:
            raise EnvError("CACHE_SIZE must be above 0 (128G, 512M, unlimited): there is no no-caching mode")
        out["cacheSize"] = size
    if raw := environ.get("BT_MAX_CONNECTIONS", "").strip():
        if not raw.isdigit() or int(raw) == 0:
            raise EnvError(f"BT_MAX_CONNECTIONS must be a whole number > 0, got {raw!r}")
        out["btMaxConnections"] = int(raw)
    if raw := environ.get("BT_DOWNLOAD_LIMIT", "").strip():
        limit = parse_size(raw)
        out["btDownloadLimit"] = 0 if limit is None else limit  # "unlimited" = 0 = no limit
    if raw := environ.get("SUBTITLES_MUX", "").strip():
        out["subtitlesMux"] = parse_bool(raw, "SUBTITLES_MUX")
    if raw := environ.get("PREFERRED_AUDIO_LANG", "").strip():
        out["preferredAudioLang"] = normalize_lang(raw)
    sizes = {
        "SEED_UPLOAD_LIMIT": "seedUploadLimit",
        "STREAM_BG_UPLOAD_LIMIT": "streamBgUploadLimit",
        "MIN_FREE_DISK": "minFreeDisk",
    }
    for var, key in sizes.items():
        if raw := environ.get(var, "").strip():
            value = parse_size(raw)
            out[key] = 0 if value is None else value  # "unlimited" = 0 = no limit
    counts = {
        "SEED_MAX_ACTIVE": "seedMaxActive",
        "BG_MAX_CONNECTIONS": "bgMaxConnections",
        "MUX_REPACK_CONCURRENCY": "muxRepackConcurrency",
    }
    for var, key in counts.items():
        if raw := environ.get(var, "").strip():
            if not raw.isdigit():
                raise EnvError(f"{var} must be a whole number >= 0, got {raw!r}")
            out[key] = int(raw)
    durations = {
        "SEED_TIME_LIMIT": "seedTimeLimit",
        "STREAM_GRACE": "streamGrace",
        "CACHE_KEEP_RECENT": "cacheKeepRecent",
    }
    for var, key in durations.items():
        if raw := environ.get(var, "").strip():
            out[key] = parse_duration(raw, var)
    if raw := environ.get("SEED_RATIO_LIMIT", "").strip():
        try:
            ratio = float(raw)
        except ValueError:
            ratio = -1
        if ratio < 0:
            raise EnvError(f"SEED_RATIO_LIMIT must be a number >= 0 (e.g. 2.0), got {raw!r}")
        out["seedRatioLimit"] = ratio
    if raw := environ.get("FONT_FALLBACK", "").strip():
        out["fontFallback"] = parse_bool(raw, "FONT_FALLBACK")
    if raw := environ.get("FALLBACK_FONTS_DIR", "").strip():
        if not os.path.isabs(raw):
            raise EnvError(f"FALLBACK_FONTS_DIR must be an absolute path, got {raw!r}")
        out["fallbackFontsDir"] = raw
    if raw := environ.get("PREFETCH_NEXT", "").strip():
        out["prefetchNext"] = parse_bool(raw, "PREFETCH_NEXT")
    if raw := environ.get("MUX_AUDIO_WAIT", "").strip():
        try:
            wait = float(raw)
        except ValueError:
            wait = -1
        if wait < 0:
            raise EnvError(f"MUX_AUDIO_WAIT must be a number of seconds >= 0, got {raw!r}")
        out["muxAudioWait"] = wait
    if raw := environ.get("FETCH_ALLOW_NETWORKS", "").strip():
        out["fetchAllowNetworks"] = parse_networks(raw, "FETCH_ALLOW_NETWORKS")
    return out


def parse_networks(value: str, name: str) -> list[str]:
    """`"192.168.1.0/24, 10.0.0.5"` → ["192.168.1.0/24", "10.0.0.5/32"] (CIDR; `,` or spaces)."""
    out = []
    for item in value.replace(",", " ").split():
        try:
            out.append(str(ipaddress.ip_network(item, strict=False)))
        except ValueError as e:
            raise EnvError(f"{name}: {item!r} is not an IP address or network (192.168.1.0/24)") from e
    return out


_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(value: str, name: str = "duration") -> float:
    """`90` / `90s` → 90, `30m`, `72h`, `7d`, `2w` → seconds; `0` = no limit."""
    v = value.strip().lower()
    unit = v[-1] if v and v[-1] in _DURATION_UNITS else ""
    try:
        n = float(v[: len(v) - len(unit)] if unit else v)
    except ValueError as e:
        raise EnvError(f"{name}: invalid duration {value!r} (examples: 0, 90, 30m, 72h, 7d)") from e
    if n < 0:
        raise EnvError(f"{name} must not be negative: {value!r}")
    return n * _DURATION_UNITS[unit]


def parse_bool(value: str, name: str) -> bool:
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise EnvError(f"{name} must be true/false, got {value!r}")


def normalize_lang(value: str) -> str:
    """`ru` / `rus` / `Russian` → `rus` (ISO 639-2, as Matroska stores it)."""
    from protoka.subs.names import LANG_TOKENS  # local import: config stays dependency-free

    v = value.strip().lower()
    if v in LANG_TOKENS:
        return LANG_TOKENS[v]
    if len(v) == 3 and v.isalpha():
        return v  # some other 639-2 code
    raise EnvError(f"PREFERRED_AUDIO_LANG: unknown language {value!r} (examples: rus, eng, jpn)")
