from sonolin.library import Library


def test_scan_finds_audio_and_skips_other_files(music_dir):
    lib = Library([music_dir])
    assert lib.scan() == 4
    assert {t.path.name for t in lib.tracks} == {"a.flac", "b.mp3", "c.m4a", "d.ogg"}


def test_scan_skips_file_and_directory_symlinks(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    track = music / "song.mp3"
    track.write_bytes(b"local track")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "private.txt"
    secret.write_bytes(b"private data")
    (outside / "other.mp3").write_bytes(b"external track")
    (music / "external.mp3").symlink_to(secret)
    (music / "internal.mp3").symlink_to(track)
    (music / "broken.mp3").symlink_to(tmp_path / "missing")
    (music / "album").symlink_to(outside, target_is_directory=True)

    lib = Library([music])
    assert lib.scan() == 1
    assert [t.path for t in lib.tracks] == [track]


def test_explicit_symlink_root_is_canonicalized(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    track = music / "song.mp3"
    track.write_bytes(b"local track")
    root = tmp_path / "selected"
    root.symlink_to(music, target_is_directory=True)
    lib = Library([root])
    assert lib.scan() == 1
    assert lib.tracks[0].path == track


def test_grouping(music_dir):
    lib = Library([music_dir]); lib.scan()
    albums = lib.albums()
    assert len(albums) == 1
    assert albums[0].artist == "Test AlbumArtist"
    assert albums[0].year == "2019"
    assert len(albums[0].sorted_tracks()) == 4
    assert lib.artists() == ["Test AlbumArtist"]
    assert lib.genres() == ["Electronic"]
    assert lib.by_artist("test albumartist")[0].name == "Test Album"


def test_search_and_stats(music_dir):
    lib = Library([music_dir]); lib.scan()
    assert len(lib.search("title")) == 4
    assert lib.search("nothing like this") == []
    assert lib.search("   ") == []
    stats = lib.stats()
    assert stats["tracks"] == 4 and stats["albums"] == 1
    assert 11 < stats["duration"] < 13


def test_hidden_folders_are_skipped(tmp_path, music_dir):
    hidden = tmp_path / ".cache"
    hidden.mkdir()
    (hidden / "x.flac").write_bytes((music_dir / "a.flac").read_bytes())
    assert Library([tmp_path]).scan() == 0


def test_add_files_indexes_one_offs_once(music_dir):
    lib = Library([])
    added = lib.add_files([music_dir / "a.flac", music_dir / "a.flac", music_dir / "notes.txt"])
    assert [t.path.name for t in added] == ["a.flac"]
    assert len(lib.tracks) == 1
    before = lib.tracks
    lib.add_files([music_dir / "b.mp3"])
    assert lib.tracks is not before, "must be a new list so the server re-indexes"
