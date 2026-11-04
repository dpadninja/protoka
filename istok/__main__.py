"""Entry point: `python -m istok` / `istok`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import platform
import signal
from pathlib import Path

import aiohttp
import libtorrent as lt
from aiohttp import web

from istok import COMPAT_SERVER_VERSION, __version__
from istok.config import EnvError, Settings, SettingsError, default_app_path, env_overrides
from istok.http_app import ServerInfo, create_app
from istok.mux import convert
from istok.session import TorrentSession
from istok.subs import fallback

log = logging.getLogger("istok")

DEFAULT_PORT = 11470
PORT_FALLBACKS = 4  # upstream: 11470 → … → 11474 → random


async def _bind(session: TorrentSession, host: str, port: int) -> tuple[web.AppRunner, ServerInfo]:
    for attempt in range(PORT_FALLBACKS + 2):
        try_port = port + attempt if attempt <= PORT_FALLBACKS else 0
        info = ServerInfo(http_port=try_port)
        runner = web.AppRunner(
            create_app(session, info, manage_session=False), handler_cancellation=True, access_log=None
        )
        await runner.setup()
        site = web.TCPSite(runner, host, try_port)
        try:
            await site.start()
        except OSError as e:
            log.warning("Cannot listen on %s: %s", try_port, e)
            await runner.cleanup()
            continue
        if runner.addresses:  # the real port when 0 (random) was requested
            info.http_port = runner.addresses[0][1]
        return runner, info
    raise SystemExit("cannot bind HTTP port")


_background: set[asyncio.Task] = set()  # fire-and-forget tasks must stay referenced


def version_banner() -> str:
    """`istok 0.3.0 (Stremio server API 4.22.5; Python 3.13.x, libtorrent 2.1.1, aiohttp 3.x)`."""
    return (
        f"istok {__version__} (Stremio server API {COMPAT_SERVER_VERSION}; "
        f"Python {platform.python_version()}, libtorrent {lt.__version__}, aiohttp {aiohttp.__version__})"
    )


async def serve(args: argparse.Namespace) -> None:
    log.info("Starting %s", version_banner())
    app_path = args.app_path or default_app_path()
    app_path.mkdir(parents=True, exist_ok=True)
    log.info("Using app path -> %s", app_path)
    settings = Settings(app_path)
    try:
        forced = env_overrides(os.environ)
        if args.bt_port is not None:
            forced["btListenPort"] = args.bt_port
        if forced:
            settings.save(forced)  # validates cacheRoot (creates it, checks it is a directory)
    except (EnvError, SettingsError) as e:
        raise SystemExit(f"configuration error: {e}") from e
    log.info(
        "Effective config: http=%s:%s bt_port=%s app_path=%s cache=%s (limit %s) uid=%s "
        "mux=%s preferred_audio=%s audio_wait=%ss repacks=%s font_fallback=%s | "
        "seeding: upload=%s bg_upload_while_watching=%s max_active=%s ratio=%s time=%ss "
        "bg_downloads=%s bg_conns=%s grace=%ss prefetch=%s min_free=%s",
        args.host,
        args.port,
        settings["btListenPort"],
        settings.app_path,
        settings.cache_dir(),
        "unlimited" if settings.cache_size == float("inf") else int(settings.cache_size),
        os.getuid(),
        settings["subtitlesMux"],
        settings["preferredAudioLang"] or "-",
        settings["muxAudioWait"],
        settings["muxRepackConcurrency"],
        settings["fallbackFontsDir"] if settings["fontFallback"] else "off",
        settings["seedUploadLimit"] or "unlimited",
        settings["streamBgUploadLimit"] or "unlimited",
        settings["seedMaxActive"] or "all",
        settings["seedRatioLimit"] or "none",
        settings["seedTimeLimit"] or "none",
        settings["bgDownloads"],
        settings["bgMaxConnections"],
        settings["streamGrace"],
        settings["prefetchNext"],
        settings["minFreeDisk"],
    )
    convert.set_concurrency(int(settings["muxRepackConcurrency"]))
    if settings["fontFallback"]:  # index the bundled fonts now, not on the first playback that needs them
        library = fallback.library(settings["fallbackFontsDir"])
        task = asyncio.create_task(asyncio.to_thread(lambda: library.faces))
        _background.add(task)
        task.add_done_callback(_background.discard)
    session = TorrentSession(settings)
    await session.start()
    runner = None
    try:
        runner, info = await _bind(session, args.host, args.port)
        log.info("EngineFS server started at http://127.0.0.1:%s", info.http_port)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        log.info("Shutting down")
    finally:
        if runner is not None:
            await runner.cleanup()
        await session.close()


def main() -> None:
    p = argparse.ArgumentParser(prog="istok", description="Stremio-compatible streaming server")
    p.add_argument("--version", action="version", version=version_banner())
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)))
    p.add_argument("--bt-port", type=int, default=None, help="BitTorrent listen port (saved to settings)")
    p.add_argument("--app-path", type=Path, default=None, help="settings/cache dir (default: $APP_PATH)")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = p.parse_args()
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()
