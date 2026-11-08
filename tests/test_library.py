"""Pure library steps: state policy, next episode, eviction."""

from __future__ import annotations

from dataclasses import replace

import pytest

from protoka.library import eviction, next_episode, policy
from protoka.library.model import Limits, State, TorrentView
from protoka.subs.names import parse_file

NOW = 1_000_000.0
LIMITS = Limits(stream_grace=120)


def view(ih: str, **kw) -> TorrentView:
    return TorrentView(ih=ih, name=ih, **kw)


def states(views, limits=LIMITS, now=NOW) -> dict[str, State]:
    return {d.ih: d.state for d in policy.decide(views, limits, now)}


# -- policy --------------------------------------------------------------------------------------
def test_streaming_and_grace():
    got = states(
        [view("a", open_streams=1), view("b", last_activity=NOW - 60), view("c", last_activity=NOW - 600)]
    )
    assert got == {"a": State.STREAMING, "b": State.STREAMING, "c": State.SEEDING}


def test_nothing_downloads_once_nobody_watches():
    """Like TorrServer and the original server: an unfinished file is not completed, what is here seeds."""
    views = [view("left", remaining_bytes=9 * 1024**3, last_activity=NOW - 600), view("full")]
    by = {d.ih: d for d in policy.decide(views, LIMITS, NOW)}
    assert by["left"].state is State.SEEDING and not by["left"].prefetch
    assert "not watched: seeding what is here, not fetching 9.00 GB of the opened files" in by["left"].reasons
    assert by["full"].reasons == ("all wanted pieces are here",)
    gone = policy.decide([view("t", prefetch_pending=True, last_activity=NOW - 600)], LIMITS, NOW)[0]
    assert gone.state is State.SEEDING and not gone.prefetch  # nor the next episode


@pytest.mark.parametrize(
    "limits,kw,expected",
    [
        (Limits(seed_ratio=1.5), {"uploaded": 160, "downloaded": 100}, State.DONE),
        (Limits(seed_ratio=1.5), {"uploaded": 100, "downloaded": 100}, State.SEEDING),
        (Limits(seed_time=3600), {"seeding_since": NOW - 7200}, State.DONE),
        (Limits(seed_time=3600), {"seeding_since": NOW - 60}, State.SEEDING),
        (
            Limits(),
            {"uploaded": 10**12, "downloaded": 1, "seeding_since": 0},
            State.SEEDING,
        ),  # no limits by default
    ],
)
def test_seed_limits(limits, kw, expected):
    assert states([view("t", **kw)], limits)["t"] is expected


def test_streaming_beats_limits():
    assert (
        states([view("t", open_streams=1, uploaded=10, downloaded=1)], Limits(seed_ratio=1))["t"]
        is State.STREAMING
    )


def test_our_ffmpeg_reading_keeps_a_torrent_downloading_without_the_next_episode():
    (d,) = policy.decide([view("t", internal_reads=1, remaining_bytes=5, prefetch_pending=True)], LIMITS, NOW)
    assert d.state is State.STREAMING and not d.prefetch
    assert d.reasons == ("our ffmpeg reads a file (audio repack)",)


def test_the_next_episode_downloads_once_the_watched_one_is_on_disk():
    (d,) = policy.decide([view("w", open_streams=1, remaining_bytes=0, prefetch_pending=True)], LIMITS, NOW)
    assert d.state is State.STREAMING and d.prefetch and "downloading the next episode" in d.reasons
    # not while the episode being watched still downloads
    (d,) = policy.decide([view("w", open_streams=1, remaining_bytes=5, prefetch_pending=True)], LIMITS, NOW)
    assert not d.prefetch
    # nor while another torrent being watched still needs the line
    busy = [
        view("w", open_streams=1, prefetch_pending=True),
        view("other", open_streams=1, remaining_bytes=1),
    ]
    by = {d.ih: d for d in policy.decide(busy, LIMITS, NOW)}
    assert not by["w"].prefetch
    assert "next episode waits: 'other' is being watched and still downloading" in by["w"].reasons


def test_every_decision_has_a_reason():
    views = [
        view("a", open_streams=1),
        view("b", remaining_bytes=1),
        view("c"),
        view("d", prefetch_pending=True),
    ]
    assert all(d.reasons for d in policy.decide(views, LIMITS, NOW))


# -- next episode --------------------------------------------------------------------------------
def files(*paths):
    return [parse_file(i, p) for i, p in enumerate(paths)]


@pytest.mark.parametrize(
    "paths,current,expected",
    [
        (["T/[G] Title - 05.mkv", "T/[G] Title - 06.mkv", "T/[G] Title - 07.mkv"], 0, 1),
        (["T/[G] Title - 06.mkv", "T/[G] Title - 05.mkv"], 1, 0),  # order in the torrent does not matter
        (["T/Title - 05.mkv", "T/Title - 05.5.mkv", "T/Title - 06.mkv"], 0, 1),  # recap episode
        (["S/Show.S01E09.mkv", "S/Show.S01E10.mkv", "S/Show.S02E01.mkv"], 0, 1),
        (["S/Show.S01E10.mkv", "S/Show.S02E01.mkv"], 0, None),  # another season is not "next"
        (["T/Title - 12.mkv", "T/Title OVA - 13.mkv"], 0, None),  # an OVA is another series
        (["T/Title - 12.mkv"], 0, None),  # last episode
        (["M/Part One.mkv", "M/Part Two.mkv", "M/Other/x.mkv"], 0, 1),  # no numbers: next in the folder
        (["T/[G] Title - 01.mkv", "T/RUS Sound/[G] Title - 02.mka", "T/[G] Title - 02.mkv"], 0, 2),
    ],
)
def test_next_video(paths, current, expected):
    got = next_episode.next_video(files(*paths), current)
    assert (got.index if got else None) == expected


def test_an_episode_is_its_video_with_subtitles_audio_and_font_sources():
    fs = files(
        "T/[G] Title - 01.mkv",
        "T/[G] Title - 02.mkv",
        "T/RUS Subs/[G] Title - 02.ass",
        "T/RUS Sound/[G] Title - 02.mka",
        "T/RUS Sound/[G] Title - 01.mka",
        "T/Fonts/Font.ttf",
        "T/fonts.rar",  # archives are not unpacked, so not part of an episode
    )
    assert next_episode.episode_files(fs, 1) == [1, 2, 3, 5]
    assert next_episode.episode_files(fs, 0) == [0, 4, 5]


# -- eviction ------------------------------------------------------------------------------------
GB = 1024**3


def plan(views, st, budget=10 * GB, reserve=0, free=100 * GB, min_free=2 * GB):
    return eviction.choose_victims(
        views, st, budget=budget, reserve=reserve, free_disk=free, min_free=min_free
    )


def test_eviction_order_done_then_lru_never_streaming():
    views = [
        view("watching", size_on_disk=4 * GB, last_watched=NOW),
        view("old", size_on_disk=3 * GB, last_watched=NOW - 9000),
        view("older", size_on_disk=3 * GB, last_watched=NOW - 99000),
        view("done", size_on_disk=2 * GB, last_watched=NOW - 10),
    ]
    st = {"watching": State.STREAMING, "old": State.SEEDING, "older": State.SEEDING, "done": State.DONE}
    p = plan(views, st, budget=8 * GB)  # used 12 GB → must free 4 GB
    assert [ih for ih, _ in p.victims] == ["done", "older"]
    assert "done seeding" in p.victims[0][1] and "least recently watched" in p.victims[1][1]
    assert p.still_short == 0


def test_eviction_counts_the_reserve_and_free_disk():
    views = [view("a", size_on_disk=1 * GB, last_watched=1), view("b", size_on_disk=1 * GB, last_watched=2)]
    st = {"a": State.SEEDING, "b": State.SEEDING}
    assert plan(views, st, budget=10 * GB).victims == []
    assert [ih for ih, _ in plan(views, st, budget=10 * GB, reserve=9 * GB).victims] == ["a"]  # 2+9 > 10
    low_disk = plan(views, st, free=int(1.5 * GB))
    assert [ih for ih, _ in low_disk.victims] == ["a"] and "low free disk" in low_disk.victims[0][1]


def test_eviction_tie_breaks_on_swarm_need_and_reports_shortfall():
    views = [
        view("needed", size_on_disk=GB, last_watched=5, seeds=1),
        view("plenty", size_on_disk=GB, last_watched=5, seeds=900),
    ]
    st = {"needed": State.SEEDING, "plenty": State.SEEDING}
    assert [ih for ih, _ in plan(views, st, budget=int(1.5 * GB)).victims] == ["plenty"]
    only_streaming = plan([view("w", size_on_disk=20 * GB)], {"w": State.STREAMING})
    assert only_streaming.victims == [] and only_streaming.still_short == 10 * GB


DAY = 86400.0


def plan_now(views, st, keep_recent=DAY, **kw):
    return eviction.choose_victims(
        views,
        st,
        budget=kw.get("budget", 50 * GB),
        reserve=kw.get("reserve", 0),
        free_disk=kw.get("free", 1400 * GB),
        min_free=kw.get("min_free", 2 * GB),
        now=NOW,
        keep_recent=keep_recent,
    )


def test_eviction_does_not_delete_for_a_reserve_it_cannot_make_room_for():
    """analysis/log.log 19:19: a 65 GB film opened with a 50 GB cache → all seeds were deleted for nothing."""
    seeds = [view(f"s{i}", size_on_disk=GB, last_watched=NOW - 3 * DAY) for i in range(3)]
    film = view("film", size_on_disk=0, last_watched=NOW)
    st = {"film": State.STREAMING, **{v.ih: State.SEEDING for v in seeds}}
    p = plan_now([film, *seeds], st, reserve=65 * GB)
    assert p.victims == []
    assert p.still_short == 18 * GB  # 3 + 65 - 50: reported, not "fixed" by deleting


def test_eviction_still_makes_room_for_a_reserve_that_fits():
    old = view("old", size_on_disk=30 * GB, last_watched=NOW - 3 * DAY)
    film = view("film", size_on_disk=0, last_watched=NOW)
    p = plan_now([film, old], {"film": State.STREAMING, "old": State.SEEDING}, reserve=25 * GB)
    assert [ih for ih, _ in p.victims] == ["old"] and p.still_short == 0


def test_eviction_frees_the_real_overage_when_the_reserve_cannot_fit():
    old = view("old", size_on_disk=10 * GB, last_watched=NOW - 3 * DAY)
    film = view("film", size_on_disk=45 * GB, last_watched=NOW)
    p = plan_now([film, old], {"film": State.STREAMING, "old": State.SEEDING}, reserve=20 * GB)
    assert [ih for ih, _ in p.victims] == ["old"]  # 55 GB on disk > 50: that much can be fixed


def test_eviction_keeps_what_was_just_watched():
    """analysis/log.log 19:40: the film stopped 2 min ago (56 GB of a 50 GB cache) was deleted at once."""
    film = view("film", size_on_disk=56 * GB, last_watched=NOW - 120)
    p = plan_now([film], {"film": State.SEEDING})
    assert p.victims == [] and p.over_used == 6 * GB
    later = plan_now([replace(film, last_watched=NOW - 2 * DAY)], {"film": State.SEEDING})
    assert [ih for ih, _ in later.victims] == ["film"]
    off = plan_now([film], {"film": State.SEEDING}, keep_recent=0)
    assert [ih for ih, _ in off.victims] == ["film"]


def test_low_disk_space_evicts_even_what_was_just_watched():
    film = view("film", size_on_disk=56 * GB, last_watched=NOW - 120)
    p = plan_now([film], {"film": State.SEEDING}, free=1 * GB)
    assert [ih for ih, _ in p.victims] == ["film"] and "low free disk" in p.victims[0][1]


def test_done_torrents_are_not_protected_by_keep_recent():
    done = view("done", size_on_disk=60 * GB, last_watched=NOW - 60)
    p = plan_now([done], {"done": State.DONE})
    assert [ih for ih, _ in p.victims] == ["done"]
