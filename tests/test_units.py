from __future__ import annotations

import json

import pytest

from protoka.config import DEFAULTS, Settings
from protoka.cors import origin_allowed
from protoka.guess import file_must_include, guess_file_idx, parse_season_episode
from protoka.stream import ByteRange, RangeNotSatisfiable, parse_range
from protoka.trackers import DEFAULT_TRACKERS, merge_trackers, parse_source


def f(path: str, length: int) -> dict:
    return {"path": path, "name": path.rsplit("/", 1)[-1], "length": length, "offset": 0}


# -- guess -----------------------------------------------------------------------------------------
class TestGuess:
    files = [
        f("Show/sample.txt", 10),
        f("Show/Show.S01E01.1080p.mkv", 100),
        f("Show/Show.S01E02.1080p.mkv", 90),
        f("Show/Show.S02E01.mkv", 300),
        f("Show/extras/making-of.mp4", 50),
    ]

    def test_largest_media_without_series_info(self):
        assert guess_file_idx(self.files, {}) == 3

    def test_episode_match(self):
        assert guess_file_idx(self.files, {"season": 1, "episode": 2}) == 2

    def test_episode_as_strings(self):
        assert guess_file_idx(self.files, {"season": "1", "episode": "1"}) == 1

    def test_no_episode_match_falls_back_to_largest(self):
        assert guess_file_idx(self.files, {"season": 5, "episode": 1}) == 3

    def test_unparsable_names_are_not_episode_matches(self):  # B14
        files = [f("a/movie-part.mkv", 500), f("a/Show.S01E03.mkv", 10)]
        assert guess_file_idx(files, {"season": 1, "episode": 3}) == 1

    def test_no_media(self):
        assert guess_file_idx([f("a.txt", 1), f("b.nfo", 2)], {}) == -1
        assert guess_file_idx([], {}) == -1
        assert guess_file_idx(self.files, None) == -1

    def test_extension_regex_is_anchored(self):  # B15
        assert guess_file_idx([f("notamkv", 10), f("x.m4v", 1)], {}) == 1

    def test_ties_pick_first(self):
        assert guess_file_idx([f("a.mkv", 5), f("b.mkv", 5)], {}) == 0

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Show.S01E02.mkv", (1, [2])),
            ("show s3e10 720p.mp4", (3, [10])),
            ("Show.S01E01E02.mkv", (1, [1, 2])),
            ("Show.S01E01-E03.mkv", (1, [1, 2, 3])),
            ("Show 2x05.avi", (2, [5])),
            ("Show Season 4 Episode 12.mkv", (4, [12])),
            ("Movie.2019.1080p.mkv", (None, [])),
        ],
    )
    def test_parse_season_episode(self, name, expected):
        assert parse_season_episode(name) == expected

    def test_file_must_include(self):
        assert file_must_include(self.files, ["S02E01"]) == 3
        assert file_must_include(self.files, ["/making-OF/i"]) == 4
        assert file_must_include(self.files, ["nothing-here"]) is None
        assert file_must_include(self.files, []) is None
        assert file_must_include(self.files, ["/[unclosed/"]) is None


# -- range -----------------------------------------------------------------------------------------
class TestRange:
    @pytest.mark.parametrize(
        "header,expected",
        [
            (None, None),
            ("", None),
            ("bytes=0-", ByteRange(0, 999)),
            ("bytes=0-0", ByteRange(0, 0)),
            ("bytes=100-199", ByteRange(100, 199)),
            ("bytes=900-5000", ByteRange(900, 999)),
            ("bytes=-100", ByteRange(900, 999)),
            ("bytes=-5000", ByteRange(0, 999)),
            ("bytes=10-20,30-40", ByteRange(10, 20)),
            ("items=0-10", None),
            ("bytes=abc", None),
        ],
    )
    def test_parse(self, header, expected):
        assert parse_range(header, 1000) == expected

    @pytest.mark.parametrize("header", ["bytes=1000-", "bytes=500-100", "bytes=-0"])
    def test_unsatisfiable(self, header):
        with pytest.raises(RangeNotSatisfiable):
            parse_range(header, 1000)


# -- cors ------------------------------------------------------------------------------------------
class TestCors:
    @pytest.mark.parametrize(
        "origin",
        [
            "https://web.stremio.com",
            "https://app.strem.io",
            "http://staging.strem.io:80",
            "https://stremio.github.io",
            "https://deploy-preview-12--stremio-development.netlify.app",
            "https://www.gstatic.com",
            "https://peario.xyz",
            "http://127.0.0.1:11470",
        ],
    )
    def test_allowed(self, origin, monkeypatch):
        monkeypatch.delenv("NO_CORS", raising=False)
        assert origin_allowed(origin, 11470)

    @pytest.mark.parametrize(
        "origin",
        [
            "https://evilstrem.io",  # B13: unanchored ".strem.io$"
            "https://peario.xyz.evil.com",  # B13: substring match
            "https://gstatic.com.evil.com",
            "https://evilstremio-development.netlify.app",
            "http://localhost:8080",
            "null",
            "file://",
            "",
            None,
        ],
    )
    def test_rejected(self, origin, monkeypatch):
        monkeypatch.delenv("NO_CORS", raising=False)
        assert not origin_allowed(origin, 11470)

    def test_no_cors_env(self, monkeypatch):
        monkeypatch.setenv("NO_CORS", "1")
        assert origin_allowed("https://anything.example", 11470)

    def test_extra_origins_env(self, monkeypatch):
        monkeypatch.delenv("NO_CORS", raising=False)
        monkeypatch.setenv("CORS_ALLOW_ORIGINS", "http://localhost:8080, https://my.app")
        assert origin_allowed("http://localhost:8080", 11470)
        assert origin_allowed("https://my.app/", 11470)


# -- trackers --------------------------------------------------------------------------------------
class TestTrackers:
    def test_parse_source(self):
        assert parse_source("tracker:udp://a:1/announce") == ("tracker", "udp://a:1/announce")
        assert parse_source("udp://a:1/announce") == ("tracker", "udp://a:1/announce")
        assert parse_source("dht:ABC") == ("dht", "abc")
        assert parse_source("tracker:ftp://x") is None
        assert parse_source(42) is None

    def test_merge_keeps_defaults_and_dedups(self):  # P9
        out = merge_trackers(["tracker:udp://custom:1/announce", "dht:x"], [DEFAULT_TRACKERS[0] + "/"])
        assert out[0] == "udp://custom:1/announce"
        assert set(DEFAULT_TRACKERS) <= {u.rstrip("/") for u in out}
        assert len(out) == len(DEFAULT_TRACKERS) + 1  # trailing-slash variant deduplicated

    def test_merge_without_defaults(self):
        assert merge_trackers(["udp://a:1"], include_defaults=False) == ["udp://a:1"]


# -- settings --------------------------------------------------------------------------------------
class TestSettings:
    def test_defaults_and_persistence(self, app_path):
        s = Settings(app_path)
        assert s["btMaxConnections"] == DEFAULTS["btMaxConnections"]
        assert s["cacheRoot"] == str(app_path)
        s.save({"btMaxConnections": 77, "custom": "kept"})
        data = json.loads((app_path / "server-settings.json").read_text())
        assert data["btMaxConnections"] == 77 and data["custom"] == "kept"
        assert Settings(app_path)["btMaxConnections"] == 77

    def test_invalid_values_fall_back(self, app_path):
        (app_path / "server-settings.json").write_text(
            json.dumps({"btMaxConnections": -3, "btHandshakeTimeout": "x", "cacheSize": "big"})
        )
        s = Settings(app_path)
        assert s["btMaxConnections"] == DEFAULTS["btMaxConnections"]
        assert s["btHandshakeTimeout"] == DEFAULTS["btHandshakeTimeout"]
        assert s["cacheSize"] == DEFAULTS["cacheSize"]

    def test_unlimited_cache(self, app_path):  # B16
        s = Settings(app_path)
        s.save({"cacheSize": None})
        assert Settings(app_path).cache_size == float("inf")

    def test_corrupt_file(self, app_path):
        (app_path / "server-settings.json").write_text("{not json")
        assert Settings(app_path)["cacheSize"] == DEFAULTS["cacheSize"]
