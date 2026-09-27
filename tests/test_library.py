from nosonpy.library import Library


def test_scan_finds_audio_and_skips_other_files(music_dir):
    lib = Library([music_dir])
    assert lib.scan() == 4
    assert {t.path.name for t in lib.tracks} == {"a.flac", "b.mp3", "c.m4a", "d.ogg"}


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
