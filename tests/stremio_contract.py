"""The `stats.json` contract as Stremio clients see it.

stremio-core deserialises the streaming server statistics strictly with serde
(`src/types/streaming_server/statistics.rs`): a missing required field or a wrong type makes the whole
response unusable, and the player then shows no peers and no speed. This module mirrors that Rust
schema type for type; `validate()` returns the list of violations (empty = Stremio can parse it).
Unknown extra fields are fine (serde ignores them by default).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Opt:
    inner: Any


@dataclass(frozen=True)
class Vec:
    inner: Any


U64, F64, STR, BOOL, URL = "u64", "f64", "String", "bool", "Url"

FILE = {"name": STR, "path": STR, "length": U64, "offset": U64}
GROWLER = {"flood": U64, "pulse": Opt(U64)}
PEER_SEARCH = {"max": U64, "min": U64, "sources": Vec(STR)}
SWARM_CAP = {"maxSpeed": Opt(F64), "minPeers": Opt(U64)}
OPTIONS = {
    "connections": Opt(U64),
    "dht": BOOL,
    "growler": GROWLER,
    "handshakeTimeout": Opt(U64),
    "path": STR,
    "peerSearch": PEER_SEARCH,
    "swarmCap": SWARM_CAP,
    "timeout": Opt(U64),
    "tracker": BOOL,
    "virtual": BOOL,
}
SOURCE = {"lastStarted": STR, "numFound": U64, "numFoundUniq": U64, "numRequests": U64, "url": URL}
STATISTICS = {
    "name": STR,
    "infoHash": STR,
    "files": Vec(FILE),
    "sources": Vec(SOURCE),
    "opts": OPTIONS,
    "downloadSpeed": F64,
    "uploadSpeed": F64,
    "downloaded": U64,
    "uploaded": U64,
    "unchoked": U64,
    "peers": U64,
    "queued": U64,
    "unique": U64,
    "connectionTries": U64,
    "peerSearchRunning": BOOL,
    "streamLen": Opt(U64),
    "streamName": Opt(STR),
    "streamProgress": Opt(F64),
    "swarmConnections": U64,
    "swarmPaused": BOOL,
    "swarmSize": U64,
}


def _check(value: Any, schema: Any, path: str, errors: list[str]) -> None:
    if isinstance(schema, Opt):
        if value is not None:
            _check(value, schema.inner, path, errors)
        return
    if value is None:
        errors.append(f"{path}: null, expected {schema if isinstance(schema, str) else 'object'}")
        return
    if isinstance(schema, Vec):
        if not isinstance(value, list):
            errors.append(f"{path}: expected array, got {type(value).__name__}")
            return
        for i, item in enumerate(value):
            _check(item, schema.inner, f"{path}[{i}]", errors)
        return
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            errors.append(f"{path}: expected object, got {type(value).__name__}")
            return
        for key, sub in schema.items():
            if key not in value:
                if not isinstance(sub, Opt):  # serde: missing Option<T> is None, anything else is an error
                    errors.append(f"{path}.{key}: missing")
                continue
            _check(value[key], sub, f"{path}.{key}", errors)
        return
    ok = {
        U64: isinstance(value, int) and not isinstance(value, bool) and value >= 0,
        F64: isinstance(value, (int, float)) and not isinstance(value, bool),
        STR: isinstance(value, str),
        BOOL: isinstance(value, bool),
        URL: isinstance(value, str) and bool(urlsplit(value).scheme),
    }[schema]
    if not ok:
        errors.append(f"{path}: {value!r} is not a valid {schema}")


def validate(stats: Any) -> list[str]:
    errors: list[str] = []
    _check(stats, STATISTICS, "stats", errors)
    return errors
