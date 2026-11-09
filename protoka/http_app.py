"""aiohttp application implementing the upstream enginefs core API (see analysis/01-api-surface.md)."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import quote

import aiohttp
import libtorrent as lt
import psutil
from aiohttp import web

from protoka import http_subtitles, logfmt
from protoka.config import settings_options
from protoka.cors import origin_allowed
from protoka.engine import Engine, EngineError
from protoka.guess import file_must_include, guess_file_idx
from protoka.http_common import (
    INFO_KEY,
    MUX_KEY,
    SESSION_KEY,
    ServerInfo,
    create_and_wait,
    error_response,
    is_internal,
    js_truthy,
    json_response,
    mime_for,
    read_body,
    redirect,
    resolve_file_idx,
    session_of,
    sources_from_options,
)
from protoka.library import next_episode
from protoka.mux import ffmpeg
from protoka.mux.service import MuxedFfmpeg, MuxedVirtual, MuxService, subtitle_service
from protoka.session import TorrentSession
from protoka.stats import engine_stats, sys_stats
from protoka.stream import FileStream, RangeNotSatisfiable, parse_range
from protoka.subs import fallback
from protoka.subs.model import Kind

log = logging.getLogger(__name__)


IH = r"{ih:[0-9a-fA-F]{40}}"
TORRENT_FETCH_LIMIT = 10 * 1024 * 1024
DEFAULT_WEBUI = "https://app.strem.io/shell-v4.4/"

DLNA_HEADERS = {
    "transferMode.dlna.org": "Streaming",
    "contentFeatures.dlna.org": "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000",  # noqa: E501
}


# -- middlewares -----------------------------------------------------------------------------------
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@web.middleware
async def cors_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    origin = request.headers.get("Origin")
    allowed = origin_allowed(origin, request.app[INFO_KEY].http_port)
    if request.method == "OPTIONS" and origin:
        preflight = web.Response(status=200)
        if allowed:
            preflight.headers["Access-Control-Allow-Origin"] = "*"
            preflight.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
            preflight.headers["Access-Control-Allow-Headers"] = request.headers.get(
                "Access-Control-Request-Headers", "Range"
            )
            preflight.headers["Access-Control-Max-Age"] = "1728000"
        return preflight
    resp: web.StreamResponse
    try:
        resp = await handler(request)
    except web.HTTPException as exc:
        resp = exc
    if allowed and not resp.prepared:
        resp.headers["Access-Control-Allow-Origin"] = "*"
    if isinstance(resp, web.HTTPException):
        raise resp
    return resp


# -- core routes -----------------------------------------------------------------------------------
async def favicon(request: web.Request) -> web.Response:
    return web.Response(status=404, content_type="application/json")


async def all_stats(request: web.Request) -> web.Response:
    ses = session_of(request)
    out: dict[str, Any] = {}
    if request.query.get("sys") == "1":
        out["sys"] = {**sys_stats(), "dht": ses.dht_report()}
    for ih, e in list(ses.engines.items()):
        if e.state == "streaming":  # the seeding library is in /seeding.json
            out[ih] = engine_stats(e)
    return json_response(out)


async def seeding_report(request: web.Request) -> web.Response:
    """GET /seeding.json — every cached torrent: state and why, ratio, swarm, cache budget, evictions."""
    return json_response(session_of(request).library.report(), headers={"Cache-Control": "no-store"})


async def engine_stats_route(request: web.Request) -> web.Response:
    e = session_of(request).get(request.match_info["ih"])
    return json_response(engine_stats(e))


async def file_stats_route(request: web.Request) -> web.Response:
    e = session_of(request).get(request.match_info["ih"])
    raw = request.match_info["idx"]
    idx = int(raw) if raw.lstrip("-").isdigit() else -1
    return json_response(engine_stats(e, idx))


async def create_by_hash(request: web.Request) -> web.Response:
    ih = request.match_info["ih"].lower()
    body = await read_body(request)
    engine = await create_and_wait(request, ih, sources=sources_from_options(body), options=body)
    stats = engine_stats(engine) or {}
    files = engine.files or []
    if files:
        must = body.get("fileMustInclude")
        if isinstance(must, list) and must:
            idx = file_must_include(files, must)
            if idx is not None:
                stats["guessedFileIdx"] = idx
        gfi = body.get("guessFileIdx")
        if js_truthy(gfi) and "guessedFileIdx" not in stats:
            stats["guessedFileIdx"] = guess_file_idx(files, gfi if isinstance(gfi, dict) else {})
    return json_response(stats)


async def create_from_torrent(request: web.Request) -> web.Response:
    body = await read_body(request)
    blob, src = body.get("blob"), body.get("from")
    try:
        if isinstance(blob, str):
            data = bytes.fromhex(blob)
        elif isinstance(src, str):
            if not src.lower().startswith(("http://", "https://")):
                # upstream read arbitrary local files here (B4)
                return error_response("'from' must be an http(s) URL", 400)
            data = await _fetch_torrent(src)
        else:
            return error_response("expected 'blob' (hex) or 'from' (URL)", 400)
        ti = lt.torrent_info(data)
    except web.HTTPException:
        raise
    except (ValueError, RuntimeError, aiohttp.ClientError, TimeoutError) as e:
        log.error("/create failed: %s", e)
        return error_response(str(e) or type(e).__name__, 500)
    ih = str(ti.info_hash()).lower()
    engine = await create_and_wait(request, ih, torrent=data)
    return json_response(engine_stats(engine))


async def _fetch_torrent(url: str) -> bytes:
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as cs, cs.get(url) as resp:
        resp.raise_for_status()
        data = await resp.content.read(TORRENT_FETCH_LIMIT + 1)
        if len(data) > TORRENT_FETCH_LIMIT:
            raise ValueError("torrent file too large")
        return data


async def remove_engine(request: web.Request) -> web.Response:
    """Stremio calls this when playback ends: the torrent stops "being watched" and keeps seeding.
    `?purge=1` deletes it from the cache."""
    await session_of(request).remove(request.match_info["ih"], purge=request.query.get("purge") == "1")
    return json_response({})


async def remove_all(request: web.Request) -> web.Response:
    # upstream answered before removal finished (B5)
    await session_of(request).remove_all(purge=request.query.get("purge") == "1")
    return json_response({})


async def stream_file(request: web.Request) -> web.StreamResponse:
    """GET/HEAD /:ih/:idx — the file, or (if the torrent has sidecar subtitles for it) the file with
    those subtitles and their fonts embedded. `?mux=0` always serves the original bytes."""
    ih = request.match_info["ih"].lower()
    q = request.query
    engine = await create_and_wait(request, ih, sources=q.getall("tr", []))
    idx = resolve_file_idx(engine, request.match_info["idx"], q.getall("f", []))
    f = engine.files[idx]  # type: ignore[index]

    if "external" in q:
        loc = f"/{ih}/{quote(f['name'], safe='')}" + ("?download=1" if "download" in q else "")
        return redirect(loc)

    headers = {
        **DLNA_HEADERS,
        "Content-Type": mime_for(f["name"]),
        "Cache-Control": "max-age=0, no-cache",
    }
    if "download" in q:
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(f['name'], safe='')}"
    if q.get("subtitles"):
        headers["CaptionInfo.sec"] = q["subtitles"]

    if not is_internal(request):
        _want_episode(engine, idx)
    muxed = None if q.get("mux") == "0" else await request.app[MUX_KEY].open(engine, idx)
    _log_opened(engine, idx, muxed, f["length"], request)
    if isinstance(muxed, MuxedFfmpeg):
        headers["Content-Type"] = "video/x-matroska"
        return await _serve_stream(request, engine, idx, headers, ffmpeg.stream(muxed.plan))
    if isinstance(muxed, MuxedVirtual):
        headers["Content-Type"] = "video/x-matroska"
        vf = muxed.file
        return await _serve_ranges(request, engine, idx, headers, vf.size, vf.iter_range)

    def original(start: int, end: int) -> AsyncIterator[bytes]:
        return aiter(FileStream(engine, idx, start, end))

    return await _serve_ranges(request, engine, idx, headers, f["length"], original)


def _want_episode(engine: Engine, idx: int) -> None:
    """A video is opened: its subtitles, external audio and font sources are wanted too, before anything
    reads them, so their pieces go to the real files, not to libtorrent's part file (analysis/18)."""
    files = subtitle_service(engine).files
    if idx >= len(files) or files[idx].kind is not Kind.VIDEO:
        return
    for fi in next_episode.episode_files(files, idx):
        engine.ensure_file_wanted(fi)


def _log_opened(engine: Engine, idx: int, muxed: object, length: int, request: web.Request) -> None:
    """INFO once per file per watching session (reset by Engine.release_memory); every request at DEBUG."""
    what = request.headers.get("Range") or "whole file"
    if is_internal(request):  # our own ffmpeg (repack / remux)
        log.debug("%s stream: read by ffmpeg (internal) %s", logfmt.tag(engine, idx), what)
        return
    if idx in engine.logged_opened:
        log.debug("%s stream: %s %s", logfmt.tag(engine, idx), request.method, what)
        return
    engine.logged_opened.add(idx)
    if isinstance(muxed, MuxedVirtual):
        s = muxed.summary
        added = []
        if s.get("audio"):
            added.append(f"{len(s['audio'])} audio")
        if s.get("tracks"):
            added.append(f"{len(s['tracks'])} subtitle")
        extra = f" ({' + '.join(added)} track(s) added)" if added else ""
        how = f"virtual MKV {logfmt.size(muxed.file.size)}{extra}"
    elif isinstance(muxed, MuxedFfmpeg):
        how = "ffmpeg remux (no seeking)"
    else:
        how = f"original file {logfmt.size(length)}"
    log.info("%s stream: opened → %s", logfmt.tag(engine, idx), how)


async def _serve_ranges(
    request: web.Request,
    engine: Engine,
    idx: int,
    headers: dict[str, str],
    size: int,
    read: Callable[[int, int], AsyncIterator[bytes]],
) -> web.StreamResponse:
    """Serve `size` bytes produced by `read(start, end)` with Range/206/416/HEAD semantics."""
    headers = {**headers, "Accept-Ranges": "bytes"}
    try:
        rng = parse_range(request.headers.get("Range"), size)
    except RangeNotSatisfiable:
        return web.Response(status=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    if rng is None:
        status, start, end = 200, 0, size - 1
    else:
        status, start, end = 206, rng.start, rng.end
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(max(0, end - start + 1))

    resp = web.StreamResponse(status=status, headers=headers)
    if request.method == "HEAD" or size == 0:
        await resp.prepare(request)
        await resp.write_eof()
        return resp
    return await _serve_stream(request, engine, idx, headers, read(start, end), resp=resp)


async def _serve_stream(
    request: web.Request,
    engine: Engine,
    idx: int,
    headers: dict[str, str],
    chunks: AsyncIterator[bytes],
    resp: web.StreamResponse | None = None,
) -> web.StreamResponse:
    """Write `chunks` to the client, keeping the engine alive while the stream is open."""
    ses = session_of(request)
    resp = resp or web.StreamResponse(status=200, headers=headers)
    if request.method == "HEAD":
        await resp.prepare(request)
        await resp.write_eof()
        return resp
    engine.ensure_file_wanted(idx)
    internal = is_internal(request)
    if internal:
        engine.internal_reads += 1
        ses.library.poke()
    else:
        ses.stream_opened(engine)
    try:
        await resp.prepare(request)
        async for chunk in chunks:
            await resp.write(chunk)
        await resp.write_eof()
    except ConnectionError:
        # The client went away (ClientConnectionResetError, BrokenPipeError…). Players do this on every
        # seek and probe — they abort the current Range request and open a new one. Not an error.
        log.debug("client closed %s/%s stream", engine.info_hash, idx)
    except EngineError as e:
        log.warning("stream %s/%s aborted: %s", engine.info_hash, idx, e)
    finally:
        aclose = getattr(chunks, "aclose", None)
        if aclose is not None:
            await aclose()
        if internal:
            engine.internal_reads -= 1
            ses.library.poke()
        else:
            ses.stream_closed(engine)
    return resp


# -- service routes ---------------------------------------------------------------------------------
def ipv4_addresses() -> list[str]:
    if os.environ.get("NO_NETWORK_INTERFACES"):
        return []
    out = []
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family.name == "AF_INET" and not a.address.startswith("127."):
                out.append(a.address)
    return out


async def heartbeat(request: web.Request) -> web.Response:
    return json_response({"success": True})


async def get_settings(request: web.Request) -> web.Response:
    ses = session_of(request)
    port = request.app[INFO_KEY].http_port
    addrs = ipv4_addresses()
    base_host = addrs[0] if addrs else "127.0.0.1"
    return json_response(
        {
            "options": settings_options(ses.settings, addrs),
            "values": ses.settings.values(),
            "baseUrl": f"http://{base_host}:{port}",
        },
        headers={"Cache-Control": "no-store"},
    )


async def post_settings(request: web.Request) -> web.Response:
    """Stremio's settings screen: accepted and ignored. The server is configured by its environment only
    (it downloads and stores for the client, so the client has nothing to decide); stremio-core still needs
    a success here, a failed POST resets its whole streaming-server state."""
    return json_response({"success": True}, headers={"Cache-Control": "no-store"})


async def network_info(request: web.Request) -> web.Response:
    return json_response({"availableInterfaces": ipv4_addresses()}, headers={"Cache-Control": "no-store"})


async def device_info(request: web.Request) -> web.Response:
    # transcoding / hw-accel profiling is not implemented in v1
    return json_response({"availableHardwareAccelerations": []}, headers={"Cache-Control": "no-store"})


async def root_redirect(request: web.Request) -> web.Response:
    host = request.headers.get("Host")
    if not host:
        return error_response("No host header", 400)
    webui = os.environ.get("WEBUI_LOCATION", DEFAULT_WEBUI)
    sep = "&" if "?" in webui else "?"
    server = quote(f"{request.scheme}://{host}", safe="")
    return redirect(f"{webui}{sep}streamingServer={server}")


# -- app factory -----------------------------------------------------------------------------------
def create_app(
    session: TorrentSession, info: ServerInfo | None = None, *, manage_session: bool = True
) -> web.Application:
    """`manage_session`: start/close the torrent session with the app (tests); the CLI manages it itself."""
    app = web.Application(middlewares=[cors_middleware], client_max_size=4 * 1024 * 1024)
    app[SESSION_KEY] = session
    app[INFO_KEY] = server_info = info or ServerInfo()

    def source_url(info_hash: str, idx: int) -> str:
        # ffmpeg reads the original (unmuxed) file through this very server
        return f"http://127.0.0.1:{server_info.http_port}/{info_hash}/{idx}?mux=0"

    settings = session.settings
    app[MUX_KEY] = MuxService(
        enabled=lambda: bool(settings["subtitlesMux"]),
        source_url=source_url,
        audio_wait=lambda: float(settings["muxAudioWait"] or 0),
        preferred_audio_lang=lambda: settings["preferredAudioLang"] or None,
        fallback_fonts=lambda: (
            fallback.library(settings["fallbackFontsDir"]) if settings["fontFallback"] else None
        ),
    )
    session.library.on_prefetched = app[MUX_KEY].prepare  # the next episode is on disk: repack its audio
    r = app.router
    # specific routes first: aiohttp matches in registration order
    r.add_get("/favicon.ico", favicon)
    r.add_get("/stats.json", all_stats)
    r.add_get("/heartbeat", heartbeat)
    r.add_get("/settings", get_settings)
    r.add_post("/settings", post_settings)
    r.add_get("/network-info", network_info)
    r.add_get("/device-info", device_info)
    r.add_get("/removeAll", remove_all)
    r.add_get("/seeding.json", seeding_report)
    r.add_route("*", "/create", create_from_torrent)
    r.add_get("/", root_redirect)
    r.add_get(f"/{IH}/stats.json", engine_stats_route)
    r.add_get(f"/{IH}/{{idx}}/stats.json", file_stats_route)
    r.add_route("*", f"/{IH}/create", create_by_hash)
    r.add_get(f"/{IH}/remove", remove_engine)
    http_subtitles.add_routes(r)
    r.add_get(f"/{IH}/{{idx}}", stream_file)
    r.add_get(f"/{IH}/{{idx}}/{{tail:.*}}", stream_file)

    if manage_session:

        async def on_startup(app: web.Application) -> None:
            await session.start()

        async def on_cleanup(app: web.Application) -> None:
            await session.close()

        app.on_startup.append(on_startup)
        app.on_cleanup.append(on_cleanup)
    return app
