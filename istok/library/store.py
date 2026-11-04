"""`config/library.json`: what libtorrent does not remember about our torrents.

    {info hash: {"name", "added_at", "last_watched", "seeding_since"}}

Upload/download totals live in libtorrent's resume data (all_time_upload/download); this file only keeps
what the policy and eviction need across restarts. Writes are atomic and debounced.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FILE = "library.json"


class LibraryStore:
    def __init__(self, config_dir: Path):
        self.path = Path(config_dir) / FILE
        self.items: dict[str, dict[str, Any]] = {}
        self.dirty = False
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if isinstance(data, dict):
                self.items = {k: v for k, v in data.items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("cannot read %s: %s", self.path, e)

    def get(self, ih: str) -> dict[str, Any]:
        item = self.items.get(ih)
        if item is None:
            item = self.items[ih] = {"added_at": time.time(), "last_watched": time.time()}
            self.dirty = True
        return item

    def update(self, ih: str, **fields: Any) -> None:
        item = self.get(ih)
        for k, v in fields.items():
            if item.get(k) != v:
                item[k] = v
                self.dirty = True

    def forget(self, ih: str) -> None:
        if self.items.pop(ih, None) is not None:
            self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=FILE + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.items, f, indent=1)
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError as e:
            Path(tmp).unlink(missing_ok=True)
            log.warning("cannot save %s: %s", self.path, e)
