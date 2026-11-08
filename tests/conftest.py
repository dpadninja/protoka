from __future__ import annotations

import logging
import os
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import libtorrent as lt
import pytest

from protoka.config import Settings
from protoka.http_app import INFO_KEY, create_app
from protoka.session import TorrentSession

OFFLINE = {
    "listen_interfaces": "127.0.0.1:0",
    "enable_dht": False,
    "enable_lsd": False,
    "enable_upnp": False,
    "enable_natpmp": False,
    "dht_bootstrap_nodes": "",
}

PIECE_SIZE = 32 * 1024


@dataclass
class Seed:
    session: lt.session
    torrent: bytes
    info_hash: str
    root: Path  # directory containing the torrent's top folder
    files: dict[str, bytes]  # name -> content

    @property
    def port(self) -> int:
        return self.session.listen_port()

    def content(self, name: str) -> bytes:
        return self.files[name]


def _wait_listening(ses: lt.session, timeout: float = 10) -> None:
    """The listen socket opens asynchronously; connecting earlier loses the first uTP SYN (~7 s retry)."""
    sockets = set()
    deadline = time.monotonic() + timeout
    while len(sockets) < 2 and time.monotonic() < deadline:
        ses.wait_for_alert(100)
        for a in ses.pop_alerts():
            if type(a).__name__ == "listen_succeeded_alert":
                sockets.add(str(a.socket_type))
    assert sockets, "seeder did not start listening"


def _wait_alert(ses: lt.session, name: str, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ses.wait_for_alert(100)
        if any(type(a).__name__ == name for a in ses.pop_alerts()):
            return
    raise AssertionError(f"seeder: no {name}")


def start_seed(root: Path, top: str, files: dict[str, bytes], *, pad_files: bool = True) -> Seed:
    """Write `files` (relative paths under `root/top`) and seed them from a local libtorrent session.

    `pad_files=False`: a v1-only torrent without pad files, so pieces straddle file boundaries (common in
    real releases)."""
    for rel, data in files.items():
        path = root / top / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        fs = lt.file_storage()
        lt.add_files(fs, str(root / top))
        flags = 0 if pad_files else lt.create_torrent.v1_only
        ct = lt.create_torrent(fs, PIECE_SIZE, flags=flags)
        lt.set_piece_hashes(ct, str(root))
        data = lt.bencode(ct.generate())
    ti = lt.torrent_info(data)
    ses = lt.session({**OFFLINE, "alert_mask": lt.alert_category.status | lt.alert_category.error})
    _wait_listening(ses)
    atp = lt.add_torrent_params()
    atp.ti = ti
    atp.save_path = str(root)
    atp.flags |= lt.torrent_flags.seed_mode
    ses.add_torrent(atp)
    _wait_alert(ses, "torrent_resumed_alert")  # the torrent starts on the next session tick (~0.5 s)
    by_name = {rel.rsplit("/", 1)[-1]: content for rel, content in files.items()}
    return Seed(ses, bytes(data), str(ti.info_hash()), root, by_name)


@pytest.fixture
def make_seed(tmp_path: Path):
    seeds: list[Seed] = []

    def factory(files: dict[str, bytes], top: str = "Show", *, pad_files: bool = True) -> Seed:
        s = start_seed(tmp_path / f"seed{len(seeds)}", top, files, pad_files=pad_files)
        seeds.append(s)
        return s

    yield factory
    for s in seeds:
        s.session.pause()


@pytest.fixture
def seed(make_seed) -> Seed:
    return make_seed(
        {
            "Show.S01E01.mkv": os.urandom(300_000),
            "Show.S01E02.mkv": os.urandom(450_123),
            "sample.txt": b"hello " * 100,
        }
    )


@pytest.fixture
def app_path(tmp_path: Path) -> Path:
    p = tmp_path / "app"
    p.mkdir()
    return p


@pytest.fixture
def settings(app_path: Path) -> Settings:
    s = Settings(app_path)
    # the host's /usr/share/fonts must not change test results: no bundled fonts unless a test adds some
    (app_path / "no-fonts").mkdir()
    s.save({"fallbackFontsDir": str(app_path / "no-fonts")})
    return s


@pytest.fixture
def torrent_session(settings: Settings) -> TorrentSession:
    return TorrentSession(settings, overrides=OFFLINE, default_trackers=False)


@pytest.fixture
async def client(aiohttp_client, torrent_session: TorrentSession):
    app = create_app(torrent_session)
    c = await aiohttp_client(app)
    app[INFO_KEY].http_port = c.port  # ffmpeg remux reads the source back through the server
    return c


def connect_to_seed(session: TorrentSession, ih: str, seed: Seed) -> None:
    e = session.get(ih)
    assert e is not None
    e.handle.connect_peer(("127.0.0.1", seed.port))


def file_index(files: list[dict], name: str) -> int:
    return next(i for i, f in enumerate(files) if f["name"] == name)


def logged(caplog, area: str) -> list[str]:
    """INFO+ messages of one log area: "[Show · file] mux: …" or "library: …"."""
    out = []
    for r in caplog.records:
        msg = r.getMessage()
        if r.levelno >= logging.INFO and (msg.startswith(f"{area}:") or f"] {area}:" in msg):
            out.append(msg)
    return out
