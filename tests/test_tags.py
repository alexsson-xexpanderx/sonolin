import io
from pathlib import Path

import pytest

from sonolin import tags


def id3_header(major, size):
    return b"ID3" + bytes([major, 0, 0]) + bytes(
        (size >> shift) & 0x7F for shift in (21, 14, 7, 0)
    )


@pytest.mark.parametrize("major", [2, 3, 4])
@pytest.mark.parametrize("size", [tags._MAX_ID3_SIZE + 1, (1 << 28) - 1])
def test_oversized_id3_is_rejected_before_reading_body(major, size):
    class HeaderOnly(io.BytesIO):
        def read(self, count=-1):
            assert self.tell() == 0 and count == 10, "must not read oversized tag body"
            return super().read(count)

    t = tags.Tags(path=Path("oversized.mp3"))
    assert tags._read_id3(HeaderOnly(id3_header(major, size)), t) == size + 10
    assert t.title == "" and t.picture is None


@pytest.mark.parametrize("major", [2, 3, 4])
@pytest.mark.parametrize("size", [tags._MAX_ID3_SIZE - 1, tags._MAX_ID3_SIZE])
def test_id3_at_size_limit_keeps_metadata_and_art(major, size):
    def frame(name, body):
        length = len(body).to_bytes(3 if major == 2 else 4, "big")
        return name + length + (b"" if major == 2 else b"\0\0") + body

    title = frame(b"TT2" if major == 2 else b"TIT2", b"\0Title")
    picture = frame(b"PIC" if major == 2 else b"APIC",
                    b"\0" + (b"PNG" if major == 2 else b"image/png\0")
                    + b"\x03\0image bytes")
    body = (title + picture).ljust(size, b"\0")
    t = tags.Tags(path=Path("allowed.mp3"))
    assert tags._read_id3(io.BytesIO(id3_header(major, size) + body), t) == size + 10
    assert t.title == "Title"
    assert t.picture == tags.Picture("image/png", b"image bytes")


def test_oversized_id3_preserves_duration_and_folder_art(tmp_path):
    path = tmp_path / "oversized.mp3"
    size = (1 << 28) - 1
    with path.open("wb") as fh:
        fh.write(id3_header(3, size))
        fh.seek(size + 10)
        # A second of MPEG-1 Layer III audio at 128 kbps, after a sparse tag.
        fh.write(b"\xff\xfb\x90\0" + b"\0" * 15996)
    t = tags.read(path, want_picture=True)
    assert t.title == "" and t.picture is None
    assert t.duration == pytest.approx(1)
    assert t.sample_rate == 44100
    assert tags.read_picture(path) is None
    (tmp_path / "cover.jpg").write_bytes(b"folder art")
    assert tags.read_picture(path) == tags.Picture("image/jpeg", b"folder art")


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
