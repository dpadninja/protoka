"""Startup checks of the CLI entry point."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from protoka.__main__ import ensure_writable


def test_ensure_writable_creates_the_directory(tmp_path: Path) -> None:
    target = tmp_path / "data" / "config"
    ensure_writable(target)
    assert target.is_dir()
    assert list(target.iterdir()) == []  # the probe file is gone


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_ensure_writable_names_the_owner_of_a_read_only_volume(tmp_path: Path) -> None:
    volume = tmp_path / "data"
    volume.mkdir(mode=0o555)
    try:
        with pytest.raises(SystemExit) as exc:
            ensure_writable(volume / "config")
    finally:
        volume.chmod(0o755)
    message = str(exc.value)
    assert f"is not writable for {os.getuid()}:{os.getgid()}" in message
    assert f"belongs to {os.getuid()}:" in message
    assert "(mode 555)" in message
    assert "--user UID:GID" in message


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_ensure_writable_reports_the_parent_it_cannot_enter(tmp_path: Path) -> None:
    """A 0770 /data of another user: even stat() of /data/config fails, so the owner of /data is reported."""
    volume = tmp_path / "data"
    (volume / "config").mkdir(parents=True)
    volume.chmod(0o000)
    try:
        with pytest.raises(SystemExit) as exc:
            ensure_writable(volume / "config")
    finally:
        volume.chmod(0o755)
    assert f"{volume} belongs to {os.getuid()}:" in str(exc.value)
    assert "(mode 0)" in str(exc.value)
