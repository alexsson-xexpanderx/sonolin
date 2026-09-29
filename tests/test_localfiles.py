import os

import pytest

from sonolin import localfiles


@pytest.mark.parametrize("kind", ["file", "directory", "fifo"])
def test_refuses_links_and_special_files(tmp_path, kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "song.mp3").write_bytes(b"private")
    track = tmp_path / "song.mp3"
    if kind == "file":
        track.symlink_to(outside / "song.mp3")
    elif kind == "directory":
        album = tmp_path / "album"
        album.symlink_to(outside, target_is_directory=True)
        track = album / "song.mp3"
    else:
        os.mkfifo(track)
    with pytest.raises(OSError):
        localfiles.open_regular(track)


def test_directory_swap_during_open_keeps_original_directory(tmp_path, monkeypatch):
    album = tmp_path / "album"
    album.mkdir()
    track = album / "song.mp3"
    track.write_bytes(b"local")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / track.name).write_bytes(b"private")
    original_open = os.open

    def swap(path, flags, **kwargs):
        if path == track.name:
            album.rename(tmp_path / "old-album")
            album.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, **kwargs)

    monkeypatch.setattr(localfiles.os, "open", swap)
    with localfiles.open_regular(track) as fh:
        assert fh.read() == b"local"
