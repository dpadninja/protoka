"""Shared pieces of the HTTP layer: app keys, response helpers and request helpers.

Imported by `http_app` (core API) and `http_subtitles` (subtitle/font endpoints).
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from aiohttp import web

from protoka import logfmt
from protoka.engine import Engine, EngineError
from protoka.guess import file_must_include, guess_file_idx
from protoka.mux.service import MuxService
from protoka.session import TorrentSession

log = logging.getLogger(__name__)

__all__ = [
    "INFO_KEY",
    "MUX_KEY",
    "SESSION_KEY",
    "ServerInfo",
    "create_and_wait",
    "error_response",
    "js_truthy",
    "json_response",
    "mime_for",
    "read_body",
    "redirect",
    "resolve_file_idx",
    "session_of",
    "sources_from_options",
]


@dataclass
class ServerInfo:
    """Mutable runtime info (the HTTP port is known only after binding)."""

    http_port: int | None = None


SESSION_KEY = web.AppKey("session", TorrentSession)
INFO_KEY = web.AppKey("info", ServerInfo)
MUX_KEY = web.AppKey("mux", MuxService)


_EXTRA_MIME = {
    ".mkv": "video/x-matroska",
    ".mk3d": "video/x-matroska",
    ".mka": "audio/x-matroska",
    ".webm": "video/webm",
    ".m4v": "video/x-m4v",
    ".avi": "video/x-msvideo",
    ".flv": "video/x-flv",
    ".ts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".srt": "application/x-subrip",
    ".vtt": "text/vtt",
    ".ass": "text/x-ssa",
    ".ssa": "text/x-ssa",
}


def mime_for(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return _EXTRA_MIME.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def json_response(data: Any, status: int = 200, **kw: Any) -> web.Response:
    return web.json_response(data, status=status, **kw)


def js_truthy(v: Any) -> bool:
    """JavaScript truthiness: {} and [] are truthy (upstream checks `body.guessFileIdx && …`)."""
    if isinstance(v, (dict, list)):
        return True
    return bool(v)


def redirect(location: str) -> web.Response:
    """307 with the Location header verbatim (HTTPTemporaryRedirect re-normalises percent-encoding)."""
    return web.Response(status=307, headers={"Location": location})


def error_response(message: str, status: int) -> web.Response:
    return json_response({"error": message}, status=status)


def session_of(request: web.Request) -> TorrentSession:
    return request.app[SESSION_KEY]


async def read_body(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    ctype = request.content_type
    try:
        if ctype == "application/json":
            body = await request.json()
        elif ctype in ("application/x-www-form-urlencoded", "multipart/form-data"):
            body = dict(await request.post())
        else:
            text = await request.text()
            if not text.strip():
                return {}
            body = json.loads(text)
    except ValueError as e:
        # the parser's message stays in the log: it can quote the body and internals (CodeQL)
        log.debug("invalid request body: %s", logfmt.clean(e))
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "invalid body"}), content_type="application/json"
        ) from e
    return body if isinstance(body, dict) else {}


def sources_from_options(opts: dict[str, Any]) -> list[Any]:
    ps = opts.get("peerSearch")
    if isinstance(ps, dict) and isinstance(ps.get("sources"), list):
        return ps["sources"]
    return []


async def create_and_wait(
    request: web.Request,
    ih: str,
    *,
    sources: list[Any] | None = None,
    torrent: bytes | None = None,
    options: dict[str, Any] | None = None,
) -> Engine:
    ses = session_of(request)
    internal = is_internal(request)
    engine = await ses.create(ih, sources=sources, torrent=torrent, options=options, touch=not internal)
    if not internal:
        ses.stream_opened(engine)  # a pending request keeps the engine alive while metadata arrives
    started = time.monotonic()
    waiting = not engine.metadata_ready.is_set()
    if waiting:
        log.info("%s stream: waiting for torrent metadata…", logfmt.tag(engine))
    try:
        await engine.wait_metadata()
        if waiting:
            n = len(engine.files or [])
            log.info(
                "%s stream: metadata ready in %s (%d files)",
                logfmt.tag(engine),
                logfmt.secs(time.monotonic() - started),
                n,
            )
    except EngineError as e:
        log.info("%s stream: torrent unavailable: %s", logfmt.tag(engine), logfmt.clean(e))
        raise web.HTTPServiceUnavailable(
            text=json.dumps({"error": "torrent unavailable"}), content_type="application/json"
        ) from e
    finally:
        if not internal:
            ses.stream_closed(engine)
    return engine


def is_internal(request: web.Request) -> bool:
    """Our own ffmpeg (audio repack, remux) reading a torrent file back through the server. Not a viewer:
    it must not keep a torrent "being watched" (a background repack would otherwise never let it go)."""
    return request.headers.get("User-Agent", "").startswith("Lavf")


def resolve_file_idx(engine: Engine, raw_idx: str, must_include: list[str]) -> int:
    files = engine.files or []
    idx: int | None = None
    if must_include:
        idx = file_must_include(files, must_include)
    if idx is None:
        if raw_idx == "-1" or raw_idx.isdigit():  # strict (B17)
            idx = int(raw_idx)
        else:
            name = unquote(raw_idx)
            idx = next((i for i, f in enumerate(files) if f["name"] == name), None)
            if idx is None:
                raise web.HTTPNotFound(
                    text='{"error": "invalid file index or file name"}', content_type="application/json"
                )
    if idx == -1:
        idx = guess_file_idx(files, {})
    if not (0 <= idx < len(files)):
        raise web.HTTPNotFound(
            text=f'{{"error": "Torrent does not contain file with index {idx}"}}',
            content_type="application/json",
        )
    return idx
