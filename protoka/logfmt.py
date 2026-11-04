"""One place for how log lines name things: `[Show · Title - 01.mkv] mux: ready in 3.1s → …`.

tag(engine)        "[Show]"                  the torrent (its name; the first 8 hex of the info hash
                                              until the metadata is known)
tag(engine, idx)   "[Show · Title - 01.mkv]" a file in it
size(n)            "1.23 GB"
secs(t)            "3.1s"
"""

from __future__ import annotations

from typing import Any


def label(engine: Any) -> str:
    name = getattr(engine, "name", None)
    return str(name) if name else str(engine.info_hash)[:8]


def file_name(engine: Any, idx: int | None) -> str | None:
    files = getattr(engine, "files", None)
    if idx is None or not files or not 0 <= idx < len(files):
        return None
    return str(files[idx]["name"])


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
