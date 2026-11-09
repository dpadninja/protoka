"""ffmpeg limits: one remux per file, and a priority queue for audio repacks."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from protoka.mux import ffmpeg
from protoka.mux.convert import BACKGROUND, URGENT, PriorityGate
from tests.media import make_video, needs_ffmpeg


async def test_gate_respects_limit_and_priority():
    gate = PriorityGate(1)
    order: list[str] = []

    async def job(name: str, priority: int, hold: asyncio.Event | None = None):
        await gate.acquire(Path(name), priority)
        order.append(name)
        try:
            if hold:
                await hold.wait()
        finally:
            gate.release()

    hold = asyncio.Event()
    first = asyncio.create_task(job("bg-running", BACKGROUND, hold))
    await asyncio.sleep(0)
    later = [asyncio.create_task(job("bg-1", BACKGROUND)), asyncio.create_task(job("bg-2", BACKGROUND))]
    await asyncio.sleep(0)
    urgent = asyncio.create_task(job("urgent", URGENT))
    await asyncio.sleep(0)
    assert gate.active == 1 and gate.queued() == 3  # the limit holds
    gate.promote(Path("bg-2"))  # the player now needs bg-2
    hold.set()
    await asyncio.gather(first, *later, urgent)
    assert order == ["bg-running", "bg-2", "urgent", "bg-1"]  # promoted first (earlier arrival), then urgent


async def test_cancelled_waiter_frees_its_place():
    gate = PriorityGate(1)
    await gate.acquire(Path("a"), URGENT)
    waiter = asyncio.create_task(gate.acquire(Path("b"), URGENT))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    gate.release()
    assert gate.active == 0 and gate.queued() == 0
    await asyncio.wait_for(gate.acquire(Path("c"), BACKGROUND), 1)


@needs_ffmpeg
async def test_a_new_remux_of_the_same_file_replaces_the_old_one(tmp_path, http_url):
    src = http_url(make_video(tmp_path / "v.avi", 20, container="avi"))
    plan = lambda: ffmpeg.FfmpegPlan(src, [], [], {})  # noqa: E731

    async def total(gen) -> int:
        return sum([len(c) async for c in gen])

    full = await total(ffmpeg.stream(plan()))
    first = ffmpeg.stream(plan())
    head = await anext(first)  # the player probes…
    second = await total(ffmpeg.stream(plan()))  # …then opens the stream again
    rest = await total(first)
    assert second == full  # the newest connection gets everything
    assert len(head) + rest < full  # the old ffmpeg was stopped instead of running to the end
    assert src not in ffmpeg._running
