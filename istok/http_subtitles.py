"""Subtitle and font endpoints — for debugging the matcher and for external players.

    GET /:ih/:idx/tracks.json             what was found for this video (subtitles, audio, fonts) and why
                                          (match.reasons), audio repack status, mux status
    GET /:ih/:idx/subtitles.json          the same (older name)
    GET /:ih/:idx/subtitles/:sid.:fmt     one subtitle as UTF-8 (original format, or converted to srt/vtt)
    GET /:ih/fonts/:font                  a font file (`12` or `13/<member of an archive>`)

Players normally do not need these: the stream itself carries the subtitles (see `mux`).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from aiohttp import web

from istok.http_common import (
    MUX_KEY,
    create_and_wait,
    error_response,
    json_response,
    mime_for,
    resolve_file_idx,
)
from istok.mux.service import subtitle_service
from istok.subs import convert
from istok.subs.ass import plain_text
from istok.subs.model import Match, SubtitleEvent
from istok.subs.names import parse_lang
from istok.subs.service import SubtitleError

__all__ = ["add_routes"]

IH = r"{ih:[0-9a-fA-F]{40}}"


def _match_json(m: Match, ih: str, idx: int) -> dict[str, Any]:
    lang, label = parse_lang(m.sub, m.video)
    fmt = m.sub.ext.lstrip(".")
    base = f"/{ih}/{idx}/subtitles/{m.sub.index}"
    return {
        "id": m.sub.index,
        "fileIdx": m.sub.index,
        "path": m.sub.path,
        "lang": lang,
        "label": label,
        "format": fmt,
        "size": m.sub.size,
        "url": f"{base}.{fmt}",
        "urls": {
            "srt": f"{base}.srt",
            "vtt": f"{base}.vtt",
            **({fmt: f"{base}.{fmt}"} if fmt in ("ass", "ssa") else {}),
        },
        "match": {"score": m.score, "reasons": [r.to_json() for r in m.reasons]},
    }


async def subtitles_json(request: web.Request) -> web.Response:
    ih = request.match_info["ih"].lower()
    engine = await create_and_wait(request, ih)
    idx = resolve_file_idx(engine, request.match_info["idx"], request.query.getall("f", []))
    subs = subtitle_service(engine)
    unmatched = [
        {
            "kind": m.sub.kind.value,
            "path": m.sub.path,
            "bestVideo": m.video.path,
            "score": m.score,
            "reasons": [r.to_json() for r in m.reasons],
        }
        for m in subs.assignment.unmatched + subs.audio_assignment.unmatched
    ]
    audio = []
    for m in subs.audio_matches(idx):
        lang, label = parse_lang(m.sub, m.video)
        audio.append(
            {
                "fileIdx": m.sub.index,
                "path": m.sub.path,
                "lang": lang,
                "label": label,
                "format": m.sub.ext.lstrip("."),
                "size": m.sub.size,
                "status": subs.audio_status.get(m.sub.index, "not built yet"),
                "match": {"score": m.score, "reasons": [r.to_json() for r in m.reasons]},
            }
        )
    return json_response(
        {
            "video": {
                "fileIdx": idx,
                "path": subs.files[idx].path,
                "episode": str(subs.files[idx].episode or ""),
            },
            "subtitles": [_match_json(m, ih, idx) for m in subs.matches(idx)],
            "audio": audio,
            "fonts": [
                {
                    "id": src.id,
                    "path": src.path,
                    "archive": src.is_archive,
                    "size": src.size,
                    "url": f"/{ih}/fonts/{src.id}",
                }
                for src in subs.font_sources(idx)
            ],
            "fontFallback": [s.as_json() for s in subs.font_fallback.get(idx, [])],
            "unmatched": unmatched,
            "mux": request.app[MUX_KEY].status(engine, idx),
            "warnings": subs.warnings,
        }
    )


async def subtitle_file(request: web.Request) -> web.Response:
    ih = request.match_info["ih"].lower()
    engine = await create_and_wait(request, ih)
    idx = resolve_file_idx(engine, request.match_info["idx"], [])
    sid, fmt = int(request.match_info["sid"]), request.match_info["fmt"].lower()
    subs = subtitle_service(engine)
    match = next((m for m in subs.matches(idx) if m.sub.index == sid), None)
    if match is None:
        return error_response(f"subtitle {sid} does not belong to file {idx}", 404)
    try:
        track = await subs.track(match)
    except SubtitleError as e:
        return error_response(str(e), 422)
    if fmt == track.source_format:
        body, ctype = track.text, mime_for(f"x.{fmt}")
    elif fmt in ("srt", "vtt"):
        events = track.events
        if track.codec_id != "S_TEXT/UTF8":
            events = [SubtitleEvent(e.start_ms, e.end_ms, plain_text(e.payload)) for e in events]
        body = convert.to_srt(events) if fmt == "srt" else convert.to_vtt(events)
        ctype = mime_for(f"x.{fmt}")
    else:
        return error_response(f"cannot convert {track.source_format} to {fmt}", 400)
    return web.Response(text=body, content_type=ctype.split(";")[0], charset="utf-8")


async def font_file(request: web.Request) -> web.Response:
    ih = request.match_info["ih"].lower()
    engine = await create_and_wait(request, ih)
    face = await subtitle_service(engine).face(request.match_info["font"])
    if face is None:
        return error_response("font not found", 404)
    return web.Response(
        body=face.data,
        content_type="font/collection" if face.filename.lower().endswith((".ttc", ".otc")) else face.mime,
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(face.filename, safe='')}"},
    )


def add_routes(router: web.UrlDispatcher) -> None:
    """Register before the catch-all stream route `/:ih/:idx/*`."""
    router.add_get(f"/{IH}/fonts/{{font:.+}}", font_file)
    router.add_get(f"/{IH}/{{idx}}/subtitles.json", subtitles_json)
    router.add_get(f"/{IH}/{{idx}}/tracks.json", subtitles_json)  # same report, a name that fits audio too
    router.add_get(f"/{IH}/{{idx}}/subtitles/{{sid:\\d+}}.{{fmt:[a-z]+}}", subtitle_file)
