"""Step 4b: ASS/SSA scripts → Matroska representation, and the font names a script needs. Pure.

Matroska stores an ASS track as (Matroska spec, "SubStation Alpha" codec mapping):
    CodecPrivate = the script header: [Script Info], [V4+ Styles] (or [V4 Styles]) and
                   "[Events]" with its "Format:" line;
    every block  = "ReadOrder,Layer,Style,Name,MarginL,MarginR,MarginV,Effect,Text" of one Dialogue,
                   with the start/end times moved into the block timestamp and BlockDuration.
`[Fonts]`/`[Graphics]` sections are dropped from the header: fonts travel as MKV attachments.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from istok.subs.model import SubtitleEvent

_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_ASS_TIME_RE = re.compile(r"^\s*(\d+):(\d{1,2}):(\d{1,2})[.:](\d{1,3})\s*$")
_FN_TAG_RE = re.compile(r"\\fn([^\\}]*)")
_DEFAULT_EVENT_FORMAT = "Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
_DROPPED_SECTIONS = {"fonts", "graphics"}


class AssError(ValueError):
    pass


def _ass_ms(value: str) -> int:
    m = _ASS_TIME_RE.match(value)
    if not m:
        raise AssError(f"bad time {value!r}")
    h, mi, s, frac = m.groups()
    # ASS uses centiseconds ("0:00:01.50"); tolerate milliseconds
    frac_ms = int(frac) * 10 if len(frac) <= 2 else int(frac[:3])
    return ((int(h) * 60 + int(mi)) * 60 + int(s)) * 1000 + frac_ms


def _sections(text: str) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = [("", [])]
    for line in text.split("\n"):
        m = _SECTION_RE.match(line)
        if m:
            out.append((m.group(1).strip(), []))
        else:
            out[-1][1].append(line)
    return out


def _format_fields(lines: list[str], default: str) -> list[str]:
    for line in lines:
        if line.lower().startswith("format:"):
            return [f.strip().lower() for f in line.split(":", 1)[1].split(",")]
    return [f.strip().lower() for f in default.split(",")]


def parse_ass(text: str) -> tuple[str, list[SubtitleEvent], bool]:
    """(codec_private, events, is_ssa). Raises AssError if there is no [Events] section."""
    sections = _sections(text)
    is_ssa = any(name.lower() == "v4 styles" for name, _ in sections)
    header_parts: list[str] = []
    events_lines: list[str] | None = None
    for name, lines in sections:
        lname = name.lower()
        if lname == "events":
            events_lines = lines
            break
        if lname in _DROPPED_SECTIONS:
            continue
        header_parts.append((f"[{name}]\n" if name else "") + "\n".join(lines).strip("\n"))
    if events_lines is None:
        raise AssError("no [Events] section")

    fields = _format_fields(events_lines, _DEFAULT_EVENT_FORMAT)
    fmt_line = next(
        (ln.strip() for ln in events_lines if ln.lower().startswith("format:")),
        "Format: " + _DEFAULT_EVENT_FORMAT,
    )
    codec_private = "\n\n".join(p for p in header_parts if p.strip()) + "\n\n[Events]\n" + fmt_line + "\n"

    idx = {f: i for i, f in enumerate(fields)}
    if "start" not in idx or "end" not in idx or "text" not in idx:
        raise AssError("Format line lacks Start/End/Text")
    n = len(fields)
    events: list[SubtitleEvent] = []
    for line in events_lines:
        if not line.lower().startswith("dialogue:"):
            continue
        raw = line.split(":", 1)[1].split(",", n - 1)
        if len(raw) < n:
            continue
        row = {name: raw[i] if name == "text" else raw[i].strip() for name, i in idx.items()}
        try:
            start, end = _ass_ms(row["start"]), _ass_ms(row["end"])
        except AssError:
            continue
        if end >= start:
            events.append(SubtitleEvent(start, end, _matroska_payload(len(events), row)))
    events.sort(key=lambda e: e.start_ms)
    return codec_private, events, is_ssa


def _matroska_payload(read_order: int, row: dict[str, str]) -> str:
    """Dialogue fields → "ReadOrder,Layer,Style,Name,MarginL,MarginR,MarginV,Effect,Text"."""
    layer = row.get("layer") or row.get("marked") or "0"
    if layer.lower().startswith("marked="):  # SSA: "Marked=0"
        layer = layer.split("=", 1)[1]
    return ",".join(
        [
            str(read_order),
            layer,
            row.get("style", ""),
            row.get("name") or row.get("actor", ""),
            row.get("marginl") or "0",
            row.get("marginr") or "0",
            row.get("marginv") or "0",
            row.get("effect", ""),
            row["text"],
        ]
    )


def font_names(text: str) -> frozenset[str]:
    """Lowercase font names used by styles (`Fontname` column) and inline `\\fn` overrides."""
    names: set[str] = set()
    for name, lines in _sections(text):
        lname = name.lower()
        if lname in ("v4+ styles", "v4 styles"):
            fields = _format_fields(lines, "Name, Fontname")
            if "fontname" not in fields:
                continue
            col = fields.index("fontname")
            for line in lines:
                if line.lower().startswith("style:"):
                    values = line.split(":", 1)[1].split(",")
                    if col < len(values):
                        names.add(values[col])
        elif lname == "events":
            for line in lines:
                if line.lower().startswith("dialogue:"):
                    names.update(_FN_TAG_RE.findall(line))
    return frozenset(n.strip().lstrip("@").strip().lower() for n in names if n.strip().lstrip("@").strip())


def plain_text(payload: str) -> str:
    """Matroska ASS block payload → readable text for SRT/VTT export (override tags removed)."""
    text = payload.split(",", 8)[-1]
    text = re.sub(r"\{[^}]*\}", "", text)
    return text.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ").strip()


# -- fonts as they are drawn: which characters each font draws, and renaming fonts ---------------------
# Used by the font fallback (subs/fallback.py): a substitute must cover what the missing font draws, and
# the script must name the substitute, because libass finds an attached font by its family name only.

_OVERRIDE_RE = re.compile(r"\{([^}]*)\}")
_TAG_RE = re.compile(r"\\(fn|fscx|r|b|i|p)([^\\]*)")


def _tag(t: str) -> tuple[str, str] | None:
    """One override tag that matters for fonts, or None (`\\bord`, `\\pos`, `\\iclip` are other tags)."""
    m = _TAG_RE.match(t)
    if not m:
        return None
    tag, arg = m.groups()
    if tag in ("b", "i", "p") and arg.strip() and not arg.strip().isdigit():
        return None
    if tag == "fscx" and arg.strip() and not arg.strip().replace(".", "", 1).isdigit():
        return None
    return tag, arg


@dataclass(frozen=True)
class Style:
    font: str  # as written (case kept), without the "@" of vertical writing
    bold: bool
    italic: bool
    scale_x: float  # percent (100 = as designed)


@dataclass(frozen=True)
class Substitute:
    """How a missing font is replaced in the script."""

    family: str
    bold: bool = False  # the missing font is bold by name: force bold where it is used
    italic: bool = False
    scale_x: float = 1.0  # width correction (0.82 = the missing font is narrower)


def _key(font: str) -> str:
    return font.strip().lstrip("@").strip().lower()


def _flag(value: str) -> bool:
    v = value.strip()
    return v not in ("", "0") and not v.startswith("0")


def _bold_tag(value: str) -> bool:
    v = value.strip()
    if not v.isdigit():
        return False
    n = int(v)
    return n == 1 or n >= 600


def _getter(col: dict[str, int], values: list[str]) -> Callable[..., str]:
    def get(f: str, default: str = "") -> str:
        i = col.get(f)
        return values[i].strip() if i is not None and i < len(values) else default

    return get


def _event_style(raw: list[str], style_col: int, st: dict[str, Style], default: Style) -> Style:
    return st.get(raw[style_col].strip().lstrip("*").lower(), default) if style_col >= 0 else default


def styles(text: str) -> dict[str, Style]:
    """Style name (lowercase) → its font and flags."""
    out: dict[str, Style] = {}
    for name, lines in _sections(text):
        if name.lower() not in ("v4+ styles", "v4 styles"):
            continue
        fields = _format_fields(lines, "Name, Fontname")
        col = {f: i for i, f in enumerate(fields)}
        if "name" not in col or "fontname" not in col:
            continue
        for line in lines:
            if not line.lower().startswith("style:"):
                continue
            values = line.split(":", 1)[1].split(",")
            get = _getter(col, values)
            try:
                scale = float(get("scalex", "100") or 100)
            except ValueError:
                scale = 100.0
            out[get("name").lower()] = Style(
                get("fontname").lstrip("@").strip(), _flag(get("bold")), _flag(get("italic")), scale
            )
    return out


def _dialogue_parts(text: str) -> tuple[list[str], int, int]:
    """(event lines, index of the Style field, number of fields) of the [Events] section."""
    for name, lines in _sections(text):
        if name.lower() == "events":
            fields = _format_fields(lines, _DEFAULT_EVENT_FORMAT)
            return lines, fields.index("style") if "style" in fields else -1, len(fields)
    return [], -1, 0


def chars_by_font(text: str) -> dict[str, dict[tuple[bool, bool], set[str]]]:
    """font (lowercase) → (bold, italic) → characters drawn with it. Drawings (`\\p1`) are not text."""
    st = styles(text)
    default = st.get("default") or next(iter(st.values()), Style("", False, False, 100.0))
    out: dict[str, dict[tuple[bool, bool], set[str]]] = {}
    lines, style_col, n = _dialogue_parts(text)
    for line in lines:
        if not line.lower().startswith("dialogue:"):
            continue
        raw = line.split(":", 1)[1].split(",", n - 1)
        if len(raw) < n:
            continue
        base = _event_style(raw, style_col, st, default)
        font, bold, italic, drawing = base.font, base.bold, base.italic, False
        body = raw[-1]
        pos = 0
        for m in [*_OVERRIDE_RE.finditer(body), None]:
            chunk = body[pos : m.start() if m else len(body)]
            if not drawing:
                visible = chunk.replace("\\N", "").replace("\\n", "").replace("\\h", " ")
                chars = {c for c in visible if not c.isspace()}
                if chars and font:
                    out.setdefault(_key(font), {}).setdefault((bold, italic), set()).update(chars)
            if m is None:
                break
            pos = m.end()
            for t in re.split(r"(?=\\)", m.group(1)):
                parsed = _tag(t)
                if parsed is None:
                    continue
                tag, arg = parsed
                if tag == "fn":
                    font = arg.strip().lstrip("@").strip() or base.font
                elif tag == "r":
                    s = st.get(arg.strip().lower(), base) if arg.strip() else base
                    font, bold, italic = s.font, s.bold, s.italic
                elif tag == "b":
                    bold = _bold_tag(arg) if arg.strip() else base.bold
                elif tag == "i":
                    italic = _flag(arg) if arg.strip() else base.italic
                elif tag == "p":
                    drawing = arg.strip().isdigit() and int(arg.strip()) > 0
    return out


def _fmt_scale(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def rename_fonts(text: str, subs: dict[str, Substitute]) -> str:
    """The script with missing fonts (lowercase keys) replaced; everything else is kept byte for byte.

    Styles get the substitute's family (keeping a vertical "@"), forced Bold/Italic and a scaled ScaleX.
    In events, `\\fn<missing>` becomes `\\fn<substitute>` followed by the forced `\\b1`/`\\i1` and a
    `\\fscx` that applies the width correction to the scale in effect; leaving such a font restores the
    scale (a forced bold/italic stays, as if the author had written it)."""
    if not subs:
        return text
    st = styles(text)
    default = st.get("default") or next(iter(st.values()), Style("", False, False, 100.0))
    out_lines: list[str] = []
    section = ""
    style_cols: dict[str, int] = {}
    _, style_col, n = _dialogue_parts(text)
    for line in text.split("\n"):
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1).strip().lower()
            out_lines.append(line)
            continue
        low = line.lower()
        if section in ("v4+ styles", "v4 styles") and low.startswith("format:"):
            style_cols = {f.strip().lower(): i for i, f in enumerate(line.split(":", 1)[1].split(","))}
        elif section in ("v4+ styles", "v4 styles") and low.startswith("style:") and "fontname" in style_cols:
            line = _rename_style(line, style_cols, subs)
        elif section == "events" and low.startswith("dialogue:") and n:
            head, rest = line.split(":", 1)
            raw = rest.split(",", n - 1)
            if len(raw) == n:
                base = _event_style(raw, style_col, st, default)
                raw[-1] = _rename_events_text(raw[-1], base, st, subs)
                line = head + ":" + ",".join(raw)
        out_lines.append(line)
    return "\n".join(out_lines)


def _rename_style(line: str, cols: dict[str, int], subs: dict[str, Substitute]) -> str:
    head, rest = line.split(":", 1)
    values = rest.split(",")
    i = cols["fontname"]
    if i >= len(values):
        return line
    sub = subs.get(_key(values[i]))
    if sub is None:
        return line
    raw = values[i]
    lead = raw[: len(raw) - len(raw.lstrip())]
    vertical = "@" if raw.strip().startswith("@") else ""
    values[i] = f"{lead}{vertical}{sub.family}"
    if sub.bold and "bold" in cols and cols["bold"] < len(values):
        values[cols["bold"]] = "-1"
    if sub.italic and "italic" in cols and cols["italic"] < len(values):
        values[cols["italic"]] = "-1"
    if sub.scale_x != 1.0 and "scalex" in cols and cols["scalex"] < len(values):
        try:
            values[cols["scalex"]] = _fmt_scale(float(values[cols["scalex"]]) * sub.scale_x)
        except ValueError:
            pass
    return head + ":" + ",".join(values)


def _rename_events_text(body: str, base: Style, st: dict[str, Style], subs: dict[str, Substitute]) -> str:
    # state: the font in effect (as a key), the scale written by the author (\fscx or the style's ScaleX)
    font = _key(base.font)
    scale = base.scale_x

    def block(m: re.Match[str]) -> str:
        nonlocal font, scale
        out: list[str] = []
        for t in re.split(r"(?=\\)", m.group(1)):
            parsed = _tag(t)
            if parsed is None:
                out.append(t)
                continue
            tag, arg = parsed
            if tag == "r":
                s = st.get(arg.strip().lower(), base) if arg.strip() else base
                font, scale = _key(s.font), s.scale_x
                out.append(t)
            elif tag == "fscx":
                try:
                    scale = float(arg.strip())
                except ValueError:
                    out.append(t)
                    continue
                sub = subs.get(font)
                out.append(f"\\fscx{_fmt_scale(scale * sub.scale_x)}" if sub and sub.scale_x != 1 else t)
            elif tag == "fn":
                prev = subs.get(font)
                name = arg.strip()
                font = _key(name) if name.lstrip("@").strip() else _key(base.font)
                sub = subs.get(font)
                if sub is not None and name.lstrip("@").strip():
                    vertical = "@" if name.startswith("@") else ""
                    out.append(f"\\fn{vertical}{sub.family}")
                else:
                    out.append(t)
                if sub is not None:
                    out.extend(["\\b1"] if sub.bold else [])
                    out.extend(["\\i1"] if sub.italic else [])
                new_ratio = sub.scale_x if sub else 1.0
                old_ratio = prev.scale_x if prev else 1.0
                if new_ratio != old_ratio:
                    out.append(f"\\fscx{_fmt_scale(scale * new_ratio)}")
            else:
                out.append(t)
        return "{" + "".join(out) + "}"

    return _OVERRIDE_RE.sub(block, body)
