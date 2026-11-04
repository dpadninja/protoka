"""Step 4a: subtitle bytes → text → events (SRT/VTT), and events → SRT/VTT text. Pure.

Old releases are rarely UTF-8: cp1251 (Russian), Shift-JIS (Japanese), UTF-16 with BOM are common.
"""

from __future__ import annotations

import codecs
import re

from charset_normalizer import from_bytes

from istok.subs.model import SubtitleEvent

_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


# Encodings seen in subtitle files; restricting detection to them avoids e.g. cp1251 read as big5.
SUBTITLE_ENCODINGS = [
    "cp1251", "koi8_r", "cp866", "cp1250", "cp1252", "iso8859_2", "cp1253", "cp1254", "cp1255", "cp1256",
    "shift_jis", "cp932", "euc_jp", "gb18030", "big5", "euc_kr", "cp949", "utf_16",
]  # fmt: skip


def decode_text(data: bytes) -> tuple[str, str]:
    """(text with \\n newlines, encoding name). BOM → strict UTF-8 → charset detection → cp1252."""
    for bom, enc in _BOMS:
        if data.startswith(bom):
            return _norm(data[len(bom) :].decode(enc, errors="replace")), enc
    try:
        return _norm(data.decode("utf-8")), "utf-8"
    except UnicodeDecodeError:
        pass
    best = from_bytes(data, cp_isolation=SUBTITLE_ENCODINGS).best() or from_bytes(data).best()
    if best is not None:
        return _norm(str(best)), best.encoding
    return _norm(data.decode("cp1252", errors="replace")), "cp1252"


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


# -- SRT / VTT parsing --------------------------------------------------------------------------
_TIME = r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
_CUE_TIME_RE = re.compile(rf"^\s*{_TIME}\s*-->\s*{_TIME}")
_VTT_TAG_RE = re.compile(r"<(?!/?(?:i|b|u)>)[^>]*>")  # keep <i>,<b>,<u>: S_TEXT/UTF8 allows them


def _ms(h: str | None, m: str, s: str, frac: str) -> int:
    return ((int(h or 0) * 60 + int(m)) * 60 + int(s)) * 1000 + int(frac.ljust(3, "0")[:3])


def _parse_cues(text: str, *, vtt: bool) -> list[SubtitleEvent]:
    events: list[SubtitleEvent] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.split("\n")
        for i, line in enumerate(lines):
            m = _CUE_TIME_RE.match(line)
            if not m:
                continue
            start = _ms(*m.groups()[:4])
            end = _ms(*m.groups()[4:])
            body = "\n".join(lines[i + 1 :]).strip()
            if vtt:
                body = _VTT_TAG_RE.sub("", body)
            if body and end > start:
                events.append(SubtitleEvent(start, end, body))
            break
    events.sort(key=lambda e: (e.start_ms, e.end_ms))
    return events


def parse_srt(text: str) -> list[SubtitleEvent]:
    return _parse_cues(text, vtt=False)


def parse_vtt(text: str) -> list[SubtitleEvent]:
    body = re.sub(r"^﻿?WEBVTT[^\n]*\n", "", text.lstrip())
    body = re.sub(r"(?ms)^(?:NOTE|STYLE|REGION)\b.*?(?:\n\s*\n|\Z)", "", body)
    return _parse_cues(body, vtt=True)


# -- rendering ----------------------------------------------------------------------------------
def _fmt(ms: int, sep: str) -> str:
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, frac = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{frac:03d}"


def to_srt(events: list[SubtitleEvent]) -> str:
    out = []
    for i, e in enumerate(events, 1):
        out.append(f"{i}\n{_fmt(e.start_ms, ',')} --> {_fmt(e.end_ms, ',')}\n{e.payload}\n")
    return "\n".join(out)


def to_vtt(events: list[SubtitleEvent]) -> str:
    cues = [f"{_fmt(e.start_ms, '.')} --> {_fmt(e.end_ms, '.')}\n{e.payload}\n" for e in events]
    return "WEBVTT\n\n" + "\n".join(cues)
