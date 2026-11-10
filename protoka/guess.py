"""File selection: GuessFileIdx and fileMustInclude (upstream lines 63278 and 18339/18497)."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any

import regex

from protoka import logfmt

log = logging.getLogger(__name__)

# Upstream regex has unescaped dots and misses some containers (B15).
MEDIA_FILE_RE = regex.compile(
    r"\.(?:mkv|avi|mp4|m4v|wmv|vp8|mov|mpg|mpeg|ts|m2ts|m3u8|webm|flv|flac|mp3|wav|wma|aac|ogg|opus)$",
    regex.IGNORECASE,
)

_EP_PATTERNS = [
    # S01E02, S01E02E03, S01E02-E03, s01.e02
    regex.compile(r"(?i)\bs(\d{1,3})[ ._-]?((?:e\d{1,4}(?:[ ._-]?)?)+(?:-e?\d{1,4})?)"),
    # 1x02, 1x02x03
    regex.compile(r"(?i)\b(\d{1,2})x(\d{1,3}(?:x\d{1,3})*)\b"),
    # Season 1 Episode 2
    regex.compile(r"(?i)season[ ._-]?(\d{1,3})[ ._-]*episode[ ._-]?(\d{1,4})"),
]

REGEX_TIMEOUT = 0.5  # seconds per match, like upstream safeStatelessRegex(…, 500)
_MAX_PATTERN_LEN = 1000


def parse_season_episode(name: str) -> tuple[int | None, list[int]]:
    for pat in _EP_PATTERNS:
        m = pat.search(name)
        if not m:
            continue
        season = int(m.group(1))
        nums = [int(n) for n in regex.findall(r"\d+", m.group(2))]
        if len(nums) == 2 and "-" in m.group(2) and nums[1] > nums[0]:
            nums = list(range(nums[0], nums[1] + 1))
        return season, nums
    return None, []


def _as_int(v: Any) -> int | None:
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    return i if i > 0 else None


def guess_file_idx(files: Sequence[dict[str, Any]] | None, series_info: dict[str, Any] | None) -> int:
    """Index of the best media file (episode match if season+episode given, else largest); -1 if none."""
    if not files or series_info is None:
        return -1
    media = [
        (i, f) for i, f in enumerate(files) if MEDIA_FILE_RE.search(f.get("path") or f.get("name") or "")
    ]
    if not media:
        return -1
    season = _as_int(series_info.get("season")) if isinstance(series_info, dict) else None
    episode = _as_int(series_info.get("episode")) if isinstance(series_info, dict) else None
    candidates = media
    if season and episode:
        matching = []
        for i, f in media:
            s, eps = parse_season_episode(f.get("path") or f.get("name") or "")
            if s == season and episode in eps:  # unparsable names are NOT a match (B14)
                matching.append((i, f))
        if matching:
            candidates = matching
    best = max(candidates, key=lambda it: (it[1].get("length") or 0, -it[0]))
    return best[0]


def compile_patterns(patterns: Iterable[Any] | None) -> list[regex.Pattern]:
    """Compile once (B7). `/re/flags` → regex with flags; plain string → regex (upstream: new RegExp(str))."""
    out: list[regex.Pattern] = []
    for p in patterns or ():
        if not isinstance(p, str) or not p or len(p) > _MAX_PATTERN_LEN:
            continue
        flags = 0
        body = p
        m = regex.fullmatch(r"/(.*)/([a-z]*)", p, flags=regex.DOTALL)
        if m:
            body = m.group(1)
            for ch in m.group(2):
                flags |= {"i": regex.IGNORECASE, "m": regex.MULTILINE, "s": regex.DOTALL}.get(ch, 0)
        try:
            out.append(regex.compile(body, flags))
        except regex.error:
            # Upstream falls back to the literal string when the /re/ form fails to compile.
            try:
                out.append(regex.compile(regex.escape(p)))
            except regex.error:
                log.debug("bad fileMustInclude pattern %s", logfmt.clean(repr(p)))
    return out


def file_must_include(files: Sequence[dict[str, Any]], patterns: Iterable[Any] | None) -> int | None:
    """First file whose name matches any pattern; None when nothing matches."""
    compiled = compile_patterns(patterns)
    if not compiled:
        return None
    for idx, f in enumerate(files):
        name = f.get("name") or ""
        for rx in compiled:
            try:
                if rx.search(name, timeout=REGEX_TIMEOUT):
                    return idx
            except TimeoutError:
                log.warning("fileMustInclude regex timed out: %s", rx.pattern)
    return None
