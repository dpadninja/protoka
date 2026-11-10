"""One place for how log lines name things: `[Show · Title - 01.mkv] mux: ready in 3.1s → …`.

tag(engine)        "[Show]"                  the torrent (its name; the first 8 hex of the info hash
                                              until the metadata is known)
tag(engine, idx)   "[Show · Title - 01.mkv]" a file in it
size(n)            "1.23 GB"
secs(t)            "3.1s"
clean(text)        client/torrent text made safe for one log line
"""

from __future__ import annotations

from typing import Any

_CONTROL = {c: f"\\x{c:02x}" for c in (*range(0x20), 0x7F)}


def clean(text: object, limit: int = 200) -> str:
    """Text from a request or a torrent, for a log line: newlines and other control characters are
    escaped (a forged line cannot be injected) and the length is capped."""
    s = str(text)
    s = s.replace("\r", "\\r").replace("\n", "\\n").translate(_CONTROL)
    return s if len(s) <= limit else s[:limit] + "…"


def label(engine: Any) -> str:
    name = getattr(engine, "name", None)
    return clean(name) if name else clean(str(engine.info_hash)[:8])


def file_name(engine: Any, idx: int | None) -> str | None:
    files = getattr(engine, "files", None)
    if idx is None or not files or not 0 <= idx < len(files):
        return None
    return clean(files[idx]["name"])


def tag(engine: Any, idx: int | None = None) -> str:
    name = file_name(engine, idx)
    return f"[{label(engine)} · {name}]" if name else f"[{label(engine)}]"


def size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def secs(t: float) -> str:
    return f"{t:.2f}s" if t < 1 else f"{t:.1f}s"
