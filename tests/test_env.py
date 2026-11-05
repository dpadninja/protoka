"""Environment-variable configuration used by container deployments (TrueNAS Custom App, docker -e)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from protoka.config import EnvError, Settings, env_overrides, parse_size


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0", 0),
        ("1073741824", 1073741824),
        ("512M", 512 * 1024**2),
        ("50G", 50 * 1024**3),
        ("50GiB", 50 * 1024**3),
        ("1.5t", int(1.5 * 1024**4)),
        ("unlimited", None),
        ("inf", None),
    ],
)
def test_parse_size(value, expected):
    assert parse_size(value) == expected


@pytest.mark.parametrize("value", ["lots", "-5G", "G"])
def test_parse_size_rejects_garbage(value):
    with pytest.raises(EnvError):
        parse_size(value)


def test_env_overrides():
    env = {"BT_PORT": "42070", "CACHE_ROOT": "/cache", "CACHE_SIZE": "20G", "UNRELATED": "x"}
    assert env_overrides(env) == {"btListenPort": 42070, "cacheRoot": "/cache", "cacheSize": 20 * 1024**3}
    assert env_overrides({}) == {}
    assert env_overrides({"BT_PORT": " ", "CACHE_SIZE": ""}) == {}


@pytest.mark.parametrize(
    "env,message",
    [
        ({"BT_PORT": "70000"}, "BT_PORT"),
        ({"BT_PORT": "abc"}, "BT_PORT"),
        ({"CACHE_ROOT": "relative/dir"}, "CACHE_ROOT"),
        ({"CACHE_SIZE": "big"}, "invalid size"),
    ],
)
def test_env_overrides_errors_name_the_variable(env, message):
    with pytest.raises(EnvError, match=message):
        env_overrides(env)


def test_overrides_win_over_saved_settings(app_path, tmp_path):
    s = Settings(app_path)
    s.save({"btListenPort": 1111, "cacheSize": 5})
    s.save(
        env_overrides({"BT_PORT": "2222", "CACHE_ROOT": str(tmp_path / "cache"), "CACHE_SIZE": "unlimited"})
    )
    again = Settings(app_path)
    assert again["btListenPort"] == 2222
    assert again.cache_size == float("inf")
    assert again.cache_dir() == tmp_path / "cache" / "stremio-cache"


def test_bad_env_stops_startup_with_a_clear_message(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "protoka", "--app-path", str(tmp_path)],
        env={"BT_PORT": "99999", "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert proc.returncode != 0
    assert "configuration error: BT_PORT" in proc.stderr


@pytest.mark.parametrize(
    "value,expected", [("ru", "rus"), ("RUS", "rus"), ("russian", "rus"), ("jpn", "jpn"), ("fil", "fil")]
)
def test_preferred_audio_lang(value, expected):
    assert env_overrides({"PREFERRED_AUDIO_LANG": value}) == {"preferredAudioLang": expected}


def test_audio_env_errors():
    with pytest.raises(EnvError, match="PREFERRED_AUDIO_LANG"):
        env_overrides({"PREFERRED_AUDIO_LANG": "klingon"})
    with pytest.raises(EnvError, match="MUX_AUDIO_WAIT"):
        env_overrides({"MUX_AUDIO_WAIT": "-3"})
    assert env_overrides({"MUX_AUDIO_WAIT": "0"}) == {"muxAudioWait": 0.0}


def test_seeding_env_vars():
    env = {
        "SEED_UPLOAD_LIMIT": "2M",
        "STREAM_BG_UPLOAD_LIMIT": "256K",
        "SEED_MAX_ACTIVE": "30",
        "SEED_RATIO_LIMIT": "2.5",
        "SEED_TIME_LIMIT": "7d",
        "BG_DOWNLOADS": "2",
        "BG_MAX_CONNECTIONS": "15",
        "STREAM_GRACE": "3m",
        "PREFETCH_NEXT": "off",
        "PREFETCH_HEAD": "16M",
        "PREFETCH_TAIL": "4M",
        "MIN_FREE_DISK": "5G",
        "MUX_REPACK_CONCURRENCY": "2",
        "CACHE_KEEP_RECENT": "12h",
    }
    assert env_overrides(env) == {
        "seedUploadLimit": 2 * 1024**2,
        "streamBgUploadLimit": 256 * 1024,
        "seedMaxActive": 30,
        "seedRatioLimit": 2.5,
        "seedTimeLimit": 7 * 86400,
        "bgDownloads": 2,
        "bgMaxConnections": 15,
        "streamGrace": 180,
        "prefetchNext": False,
        "prefetchHead": 16 * 1024**2,
        "prefetchTail": 4 * 1024**2,
        "minFreeDisk": 5 * 1024**3,
        "muxRepackConcurrency": 2,
        "cacheKeepRecent": 12 * 3600,
    }
    assert env_overrides({"SEED_UPLOAD_LIMIT": "unlimited"}) == {"seedUploadLimit": 0}


@pytest.mark.parametrize(
    "env,message",
    [
        ({"SEED_TIME_LIMIT": "soon"}, "SEED_TIME_LIMIT"),
        ({"SEED_RATIO_LIMIT": "-1"}, "SEED_RATIO_LIMIT"),
        ({"BG_DOWNLOADS": "two"}, "BG_DOWNLOADS"),
        ({"PREFETCH_NEXT": "maybe"}, "PREFETCH_NEXT"),
    ],
)
def test_seeding_env_errors(env, message):
    with pytest.raises(EnvError, match=message):
        env_overrides(env)


def test_old_default_cache_size_is_migrated(app_path):
    import json

    (app_path / "server-settings.json").write_text(json.dumps({"cacheSize": 2147483648}))
    s = Settings(app_path)
    assert s["cacheSize"] == 50 * 1024**3 and s["settingsVersion"] == 2
    s.save({"cacheSize": 2147483648})  # chosen explicitly after the migration: kept
    assert Settings(app_path)["cacheSize"] == 2147483648


def test_version_is_logged_at_start(tmp_path):
    """The first log line names the running version (from the package metadata = pyproject.toml)."""
    import protoka

    proc = subprocess.run(
        [sys.executable, "-m", "protoka", "--app-path", str(tmp_path)],
        env={"BT_PORT": "99999", "PATH": "/usr/bin:/bin"},  # stops right after the banner (bad config)
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert f"Starting protoka {protoka.__version__} " in proc.stderr
    out = subprocess.run([sys.executable, "-m", "protoka", "--version"], capture_output=True, text=True)
    assert out.stdout.startswith(f"protoka {protoka.__version__} (Stremio server API")


def test_font_fallback_env():
    out = env_overrides({"FONT_FALLBACK": "off", "FALLBACK_FONTS_DIR": "/opt/fonts"})
    assert out == {"fontFallback": False, "fallbackFontsDir": "/opt/fonts"}
    with pytest.raises(EnvError, match="FALLBACK_FONTS_DIR"):
        env_overrides({"FALLBACK_FONTS_DIR": "fonts"})
    with pytest.raises(EnvError, match="FONT_FALLBACK"):
        env_overrides({"FONT_FALLBACK": "maybe"})
