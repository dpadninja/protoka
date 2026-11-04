"""Test media generated on the fly: MKV/AVI files (ffmpeg), fonts (fontTools), subtitle scripts."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not installed")


LATIN = " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.,!?'-"
CYRILLIC = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя"
KANJI = "漢字日本語"


def make_font(family: str, chars: str = "A", *, bold: bool = False, italic: bool = False) -> bytes:
    """A tiny TrueType font: one square glyph per character of `chars`."""
    names = [f"uni{ord(c):04X}" for c in dict.fromkeys(chars)]
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder([".notdef", *names])
    fb.setupCharacterMap({ord(c): f"uni{ord(c):04X}" for c in chars})
    pen = TTGlyphPen(None)
    pen.moveTo((0, 0))
    pen.lineTo((0, 500))
    pen.lineTo((500, 500))
    pen.closePath()
    glyph = pen.glyph()
    fb.setupGlyf({n: glyph for n in [".notdef", *names]})
    fb.setupHorizontalMetrics({n: (500, 0) for n in [".notdef", *names]})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    style = ("Bold " if bold else "") + ("Italic" if italic else "") or "Regular"
    fb.setupNameTable({"familyName": family, "styleName": style.strip()})
    fb.setupOS2(usWeightClass=700 if bold else 400, fsSelection=(1 if italic else 0) | (32 if bold else 0))
    fb.setupPost()
    fb.updateHead(macStyle=(1 if bold else 0) | (2 if italic else 0))
    buf = io.BytesIO()
    fb.save(buf)
    return buf.getvalue()


def write_fallback_fonts(d: Path) -> Path:
    """A fake /usr/share/fonts with the bundled families and realistic coverage (Liberation has no CJK…)."""
    fonts = {
        "noto/NotoSans-Regular.ttf": make_font("Noto Sans", LATIN + CYRILLIC),
        "noto/NotoSans-Bold.ttf": make_font("Noto Sans", LATIN + CYRILLIC, bold=True),
        "noto/NotoSerif-Regular.ttf": make_font("Noto Serif", LATIN + CYRILLIC),
        "noto/NotoSerif-Bold.ttf": make_font("Noto Serif", LATIN + CYRILLIC, bold=True),
        "noto/NotoSerif-Italic.ttf": make_font("Noto Serif", LATIN + CYRILLIC, italic=True),
        "noto/NotoSansMono-Regular.ttf": make_font("Noto Sans Mono", LATIN + CYRILLIC),
        "noto/NotoSansCJKjp-Regular.ttf": make_font("Noto Sans CJK JP", LATIN + CYRILLIC + KANJI),
        "noto/NotoSerifCJKjp-Regular.ttf": make_font("Noto Serif CJK JP", LATIN + KANJI),
        "liberation/LiberationSans-Regular.ttf": make_font("Liberation Sans", LATIN + CYRILLIC),
        "liberation/LiberationSerif-Regular.ttf": make_font("Liberation Serif", LATIN),  # no Cyrillic here
        "Other.ttf": make_font("Some Host Font", LATIN + CYRILLIC),  # not a bundled family: ignored
    }
    for name, data in fonts.items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(data)
    return d


def make_video(path: Path, seconds: int, container: str = "mkv") -> Path:
    """Small test video with 1 s keyframe/cluster spacing (so the MKV has a cue per second)."""
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000",
            "-t", str(seconds), "-c:v", "mpeg4", "-g", "10", "-q:v", "10"]  # fmt: skip
    if container == "mkv":
        args += ["-c:a", "aac", "-b:a", "16k", "-cluster_time_limit", "1000"]
    else:
        args += ["-c:a", "pcm_s16le"]
    subprocess.run([*args, str(path)], check=True)
    return path


def ass_script(lines: list[tuple[str, str, str]], font: str = "Test Sans") -> str:
    """ASS script; lines = [(start "0:00:01.00", end, text)]."""
    events = "\n".join(f"Dialogue: 0,{s},{e},Default,,0,0,0,,{t}" for s, e, t in lines)
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: 160
PlayResY: 120

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
{events}
"""  # noqa: E501


def srt_script(lines: list[tuple[str, str, str]]) -> str:
    return "\n".join(f"{i}\n{s} --> {e}\n{t}\n" for i, (s, e, t) in enumerate(lines, 1))


def probe(path_or_url: str) -> dict:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_streams", "-of", "json", path_or_url],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    return json.loads(out.stdout)


def subtitle_dump(path: str, stream: int, seek: float | None = None, fmt: str = "srt") -> str:
    args = [FFMPEG, "-hide_banner", "-loglevel", "error"]
    if seek is not None:
        args += ["-ss", str(seek)]
    args += ["-i", path, "-map", f"0:s:{stream}", "-f", fmt, "-"]
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def packets_md5(path: str, selector: str) -> str:
    out = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", path, "-map", selector, "-c", "copy", "-f", "md5", "-"],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    return out.stdout.strip()


def make_audio(path: Path, seconds: int, codec: str = "flac", freq: int = 330) -> Path:
    """Sidecar audio: `.mka` (Matroska, clusters of 1 s, CRC-32 like ffmpeg/mkvmerge write) or a raw stream."""
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000", "-t", str(seconds), "-c:a", codec]  # fmt: skip
    if path.suffix == ".mka":
        args += ["-cluster_time_limit", "1000"]
    subprocess.run([*args, str(path)], check=True)
    return path


def ffmpeg_errors(path: str, *extra: str) -> str:
    """stderr of a full demux pass (`-err_detect crccheck` reports bad CRC-32 elements)."""
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-v", "error", *extra, "-i", path, "-map", "0", "-c", "copy", "-f", "null", "-"],
        capture_output=True, text=True,
    )  # fmt: skip
    return proc.stderr.strip()
