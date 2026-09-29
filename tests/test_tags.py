import os

import pytest

from sonolin import tags


@pytest.mark.parametrize("name", ["a.flac", "b.mp3", "c.m4a", "d.ogg"])
def test_every_format_reads_the_same_tags(music_dir, name):
    t = tags.read(music_dir / name)
    assert (t.title, t.artist, t.album) == ("Test Title", "Test Artist", "Test Album")
    assert t.album_artist == "Test AlbumArtist"
    assert t.genre == "Electronic"
    assert t.composer == "Test Composer"
    assert t.year == "2019"
    assert (t.track, t.disc) == (7, 2)
    assert t.duration == pytest.approx(3.0, abs=0.1)
    assert t.sample_rate == 44100


@pytest.mark.parametrize("name", ["a.flac", "b.mp3", "c.m4a"])
def test_embedded_cover_art(music_dir, name):
    pic = tags.read_picture(music_dir / name)
    assert pic is not None
    assert pic.mime == "image/png"
    assert pic.data.startswith(b"\x89PNG")


def test_picture_is_not_kept_by_default(music_dir):
    assert tags.read(music_dir / "a.flac").picture is None


def test_folder_cover_is_the_fallback(tmp_path, music_dir):
    track = tmp_path / "x.ogg"
    track.write_bytes((music_dir / "d.ogg").read_bytes())
    (tmp_path / "cover.jpg").write_bytes(b"\xff\xd8\xff fake jpeg")
    pic = tags.read_picture(track)
    assert pic is not None and pic.mime == "image/jpeg"


@pytest.mark.parametrize("name", ["cover", "folder", "front", "album", "albumart"])
@pytest.mark.parametrize("ext", [".jpg", ".jpeg", ".png"])
def test_cover_symlinks_are_ignored(tmp_path, name, ext):
    music = tmp_path / "music"
    music.mkdir()
    track = music / "track.ogg"
    track.touch()
    secret = tmp_path / "private.txt"
    secret.write_bytes(b"private data outside the music folder")
    (music / f"{name}{ext}").symlink_to(secret)

    assert tags.read_picture(track) is None


@pytest.mark.parametrize("kind", ["symlink", "broken_symlink", "directory", "fifo"])
def test_unsafe_cover_does_not_hide_later_regular_cover(tmp_path, kind):
    track = tmp_path / "track.ogg"
    track.touch()
    cover = tmp_path / "cover.jpg"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(b"not the cover")
        cover.symlink_to(target)
    elif kind == "broken_symlink":
        cover.symlink_to(tmp_path / "missing")
    elif kind == "directory":
        cover.mkdir()
    else:
        os.mkfifo(cover)
    (tmp_path / "folder.png").write_bytes(b"regular cover")

    assert tags.read_picture(track) == tags.Picture("image/png", b"regular cover")


@pytest.mark.parametrize("suffix", [".flac", ".mp3", ".m4a", ".ogg"])
def test_garbage_yields_sparse_tags_not_an_exception(tmp_path, suffix):
    bad = tmp_path / f"broken{suffix}"
    bad.write_bytes(b"\x00\xff" * 500 + b"ID3\x04\x00\x00\x7f\x7f\x7f\x7f" + b"fLaC")
    t = tags.read(bad)
    assert t.display_title == "broken"
    assert t.display_artist == "Unknown Artist"


def test_missing_file_is_not_an_exception(tmp_path):
    assert tags.read(tmp_path / "gone.flac").title == ""


def test_vorbis_track_number_forms():
    assert tags._int("3/12") == 3
    assert tags._int(" 04 ") == 4
    assert tags._int("") == 0
    assert tags._int("x") == 0
