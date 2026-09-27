"""Shared fixtures. Nothing here needs a Sonos speaker or a network."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# Widget tests run headless; this must be set before Qt is first imported.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

COMMON_TAGS = {
    "title": "Test Title", "artist": "Test Artist", "album": "Test Album",
    "album_artist": "Test AlbumArtist", "genre": "Electronic",
    "composer": "Test Composer", "date": "2019", "track": "7/12", "disc": "2/3",
}


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


@pytest.fixture(scope="session")
def music_dir(tmp_path_factory) -> Path:
    """One tagged, three-second file per supported format, most with cover art."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is needed to generate test audio")
    d = tmp_path_factory.mktemp("music")
    cover = d / "cover.png"
    tone = d / "tone.wav"
    _ffmpeg("-f", "lavfi", "-i", "color=c=teal:s=64x64", "-frames:v", "1", str(cover))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-ac", "2", "-ar", "44100",
            str(tone))
    meta = [x for k, v in COMMON_TAGS.items() for x in ("-metadata", f"{k}={v}")]
    art = ["-i", str(cover), "-map", "0:a", "-map", "1:v", "-c:v", "copy",
           "-disposition:v", "attached_pic"]
    _ffmpeg("-i", str(tone), *art, *meta, str(d / "a.flac"))
    _ffmpeg("-i", str(tone), *art, "-c:a", "libmp3lame", "-b:a", "128k",
            "-id3v2_version", "3", *meta, str(d / "b.mp3"))
    _ffmpeg("-i", str(tone), *art, "-c:a", "alac", *meta, str(d / "c.m4a"))
    _ffmpeg("-i", str(tone), "-c:a", "libvorbis", *meta, str(d / "d.ogg"))
    tone.unlink()
    cover.unlink()
    (d / "notes.txt").write_text("not audio")
    return d


@pytest.fixture(autouse=True)
def _private_dirs(tmp_path_factory, monkeypatch) -> None:
    """Every test gets throwaway config and cache folders, so none can write to
    the real ~/.config/nosonpy or ~/.cache/nosonpy (artwork, settings)."""
    root = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(root / "cache"))


@pytest.fixture
def xdg(tmp_path, monkeypatch) -> Path:
    """Point the config at a throwaway directory so tests never touch ~/.config."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path
