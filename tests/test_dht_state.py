"""DHT routing table persistence: counting, loading, the empty-state guard and the save schedule."""

from __future__ import annotations

import asyncio
import os
import socket
import struct

import libtorrent as lt
import pytest

from protoka import session as session_mod
from protoka.config import Settings
from protoka.session import STATE_FILE, TorrentSession, count_saved_nodes
from tests.conftest import OFFLINE


def saved_state(n4: int, n6: int = 0) -> bytes:
    """A state buffer shaped like libtorrent's own: node-id = [ip(4)+id(20)], nodes = compact endpoints."""
    v4 = [socket.inet_aton(f"10.0.0.{i + 1}") + struct.pack(">H", 6881) for i in range(n4)]
    v6 = [
        socket.inet_pton(socket.AF_INET6, f"2001:db8::{i + 1}") + struct.pack(">H", 6881) for i in range(n6)
    ]
    dht = {b"node-id": [socket.inet_aton("127.0.0.1") + os.urandom(20)], b"nodes": v4}
    if v6:
        dht[b"nodes6"] = v6
    return bytes(lt.bencode({b"dht state": dht}))


def test_count_saved_nodes():
    assert count_saved_nodes(saved_state(2, 1)) == 3
    assert count_saved_nodes(saved_state(0)) == 0
    assert count_saved_nodes(b"garbage") == 0
    assert count_saved_nodes(bytes(lt.bencode({b"other": 1}))) == 0


def test_saved_state_is_loaded_at_start(app_path):
    (app_path / STATE_FILE).write_bytes(saved_state(5, 2))
    ses = TorrentSession(Settings(app_path), overrides=OFFLINE, default_trackers=False)
    assert ses.dht_nodes_loaded == 7
    report = ses.dht_report()
    assert report["nodesLoadedAtStart"] == 7 and report["stateFile"].endswith(STATE_FILE)


def test_first_run_writes_a_state_file(torrent_session: TorrentSession):
    assert torrent_session.dht_nodes_loaded == 0
    assert not torrent_session.state_path.exists()
    assert torrent_session.save_state() is True  # nothing to protect yet: write even an empty table
    assert torrent_session.state_path.exists()
    assert torrent_session.dht_report()["stateSavedAt"] is not None


def test_empty_routing_table_does_not_overwrite_a_saved_one(app_path):
    good = saved_state(10)
    (app_path / STATE_FILE).write_bytes(good)
    ses = TorrentSession(Settings(app_path), overrides=OFFLINE, default_trackers=False)  # DHT off → 0 nodes
    assert ses.save_state() is False
    assert (app_path / STATE_FILE).read_bytes() == good


async def test_periodic_saves_on_schedule(torrent_session: TorrentSession, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(session_mod, "STATE_FIRST_SAVE", 0.05)
    monkeypatch.setattr(session_mod, "STATE_SAVE_INTERVAL", 0.1)
    calls = []
    monkeypatch.setattr(torrent_session, "save_state", lambda: calls.append(1) or True)
    task = asyncio.create_task(torrent_session._periodic())
    try:
        await asyncio.sleep(0.02)
        assert calls == []  # not before the first-save delay
        await asyncio.sleep(0.4)
        assert 2 <= len(calls) <= 6
    finally:
        task.cancel()


async def test_stats_sys_reports_dht(client):
    data = await (await client.get("/stats.json?sys=1")).json()
    assert set(data["sys"]["dht"]) == {"nodes", "nodesLoadedAtStart", "stateSavedAt", "stateFile"}
