"""Piece reads around libtorrent's part file (data of priority-0 files) — a local seeder, offline."""

from __future__ import annotations

import asyncio
import os

from protoka.engine import Engine
from protoka.session import TorrentSession
from tests.conftest import PIECE_SIZE, Seed, connect_to_seed, file_index

T = 30


def _torrent_bytes(engine: Engine, seed: Seed) -> bytes:
    files = sorted(engine.files or [], key=lambda f: f["offset"])
    return b"".join(seed.content(f["name"]) for f in files)


async def test_a_prefetched_boundary_piece_reads_right_after_its_file_is_opened(
    client, make_seed, torrent_session: TorrentSession
) -> None:
    """Prefetch (or reading an .mka index) downloads pieces of files that are still priority 0, so libtorrent
    keeps them in its part file. Opening the video and its audio (priority 0 → 4) moves them out about a
    second later; reads of a piece shared by the two fail meanwhile, and libtorrent stops the torrent with
    "file too short" / ENOENT. Reads must wait that out (they used to give up: "cannot read piece")."""
    seed = make_seed(
        {
            "Ep 07.mkv": os.urandom(PIECE_SIZE * 10 + 1234),
            "Ep 08.mkv": os.urandom(PIECE_SIZE * 10 + 777),
            "Rus sound/Ep 08.mka": os.urandom(PIECE_SIZE * 6 + 999),
            "Rus sound/Ep 09.mka": os.urandom(PIECE_SIZE * 6 + 555),
        },
        pad_files=False,
    )
    engine = await torrent_session.create(seed.info_hash, torrent=seed.torrent)
    connect_to_seed(torrent_session, seed.info_hash, seed)
    files = engine.files
    assert files is not None
    mka = file_index(files, "Ep 08.mka")
    plen = engine.piece_length
    assert files[mka]["offset"] % plen, "the test needs a piece shared with the neighbouring file"
    pieces = sorted(set(engine.file_piece_range(mka)))  # head and tail, both shared with neighbours
    ep07 = file_index(files, "Ep 07.mkv")
    engine.ensure_file_wanted(ep07)  # episode 07 is being watched
    for p in pieces:
        engine.handle.piece_priority(p, 7)  # what the prefetch does; the files stay at priority 0
    first07, last07 = engine.file_piece_range(ep07)
    async with asyncio.timeout(T):
        for p in [*pieces, *range(first07, last07 + 1)]:
            await engine.wait_piece(p)

    engine.ensure_file_wanted(file_index(files, "Ep 08.mkv"))  # the episode is opened…
    engine.ensure_file_wanted(mka)  # …and its audio is repacked from a stream of the .mka
    whole = _torrent_bytes(engine, seed)
    async with asyncio.timeout(T):
        for p in pieces:
            assert await engine.read_piece(p) == whole[p * plen : (p + 1) * plen]
        # libtorrent may have stopped the torrent meanwhile: it must be running again, the video complete
        video = file_index(files, "Ep 08.mkv")
        first, last = engine.file_piece_range(video)
        for p in range(first, last + 1):
            await engine.wait_piece(p)
        while engine.error is not None or engine.handle.status().errc.value():
            await asyncio.sleep(0.1)


class _Handle:
    """The real handle, with clear_error() counted instead of called."""

    def __init__(self, real) -> None:
        self.real = real
        self.cleared = 0

    def clear_error(self) -> None:
        self.cleared += 1

    def __getattr__(self, name):
        return getattr(self.real, name)


async def test_a_burst_of_storage_errors_resumes_the_torrent_once_and_gives_up_eventually(
    client, seed: Seed, torrent_session: TorrentSession, monkeypatch, caplog
) -> None:
    engine = await torrent_session.create(seed.info_hash, torrent=seed.torrent)
    monkeypatch.setattr("protoka.engine.ERROR_RECOVERY_DELAY", 0.05)
    handle = _Handle(engine.handle)
    engine.handle = handle  # type: ignore[assignment]
    alert = type(
        "torrent_error_alert", (), {"message": lambda self: "x: (2 No such file or directory) /a.mka"}
    )()

    async def burst() -> None:
        for _ in range(30):  # one read failure each, like the log of the real case
            engine.on_alert(alert)
        await asyncio.sleep(0.2)

    try:
        await burst()
        assert handle.cleared == 1
        assert engine.error is None
        await burst()
        await burst()
        assert handle.cleared == 3
        await burst()  # the 4th within a minute: a real problem, the error stays
        assert handle.cleared == 3
        assert engine.error is not None
        errors = [
            r for r in caplog.records if r.name == "protoka.engine" and r.levelname in ("WARNING", "ERROR")
        ]
        assert len(errors) == 4  # one line per burst, not per alert
    finally:
        engine.handle = handle.real
