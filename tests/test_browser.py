"""Browser logic that does not need a speaker or a network."""

from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from sonolin.gui.browser import (  # noqa: E402
    Browser, _is_icon, fmt_duration, track_id_from_uri,
)
from sonolin.services import Entry, ServiceInfo  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_now_playing_uri_matches_service_ids():
    uri = "x-sonos-spotify:spotify%3atrack%3a6GyFP1nfCDB8lbD2bG0Hq9?sid=9&flags=8232&sn=2"
    assert track_id_from_uri(uri) == "spotify:track:6GyFP1nfCDB8lbD2bG0Hq9"
    assert track_id_from_uri("") == ""
    assert track_id_from_uri("garbage") == ""


def test_durations():
    assert fmt_duration(243) == "4:03"
    assert fmt_duration(3725) == "1:02:05"
    assert fmt_duration(4380, long=True) == "1 h 13 min"
    assert fmt_duration(620, long=True) == "10 min"


def _image(fill, shape=None):
    img = QImage(64, 64, QImage.Format.Format_RGB32)
    img.fill(QColor(fill))
    if shape:
        p = QPainter(img)
        p.fillRect(20, 20, 24, 24, QColor(shape))
        p.end()
    return img


def test_icon_detection(app):
    assert _is_icon(_image("black", "white")), "white glyph on black is an icon"
    assert not _is_icon(_image("#c0392b")), "a coloured cover is not"
    assert not _is_icon(_image("#f0f0f0", "black")), "a light image is not"


def _browser(app):
    return Browser(services=SimpleNamespace(), speaker=lambda: None)


def test_service_list_groups_and_defaults(app):
    b = _browser(app)
    cat = [ServiceInfo("Apple Music", "AppLink", 1, 1), ServiceInfo("Spotify", "AppLink", 9, 2311),
           ServiceInfo("TuneIn", "Anonymous", 254, 65031), ServiceInfo("Radio X", "Anonymous", 3, 3)]
    b.load_services(cat, linked={"Spotify", "TuneIn", "Radio X"}, preferred="")
    items = [b.service.itemText(i) for i in range(b.service.count())]
    assert items == ["Spotify", "", "TuneIn", "Radio X", "", "Apple Music"]
    assert b.name == "Spotify", "a linked account wins by default"
    b.load_services(cat, linked={"TuneIn", "Radio X"}, preferred="")
    assert b.name == "TuneIn", "with no linked account, TuneIn"
    b.load_services(cat, linked={"Spotify", "TuneIn"}, preferred="Radio X")
    assert b.name == "Radio X", "the remembered service wins"


def _entry(kind, **kw):
    base = dict(item=object(), kind=kind, title=f"a {kind}", playable=True,
                openable=kind != "track")
    base.update(kw)
    return Entry(**base)


def test_menu_for_a_song(app):
    b = _browser(app)
    b._playlists = [SimpleNamespace(title="Kitchen"), SimpleNamespace(title="Top List")]
    menu = b.build_menu([_entry("track", artist="M83", artist_id="a:1",
                                album="Junk", album_id="b:2")])
    texts = [a.text() for a in menu.actions()]
    assert texts[:4] == ["▶  Play", "⏭  Play next", "＋  Add to queue", "♫  Add to playlist"]
    assert "Go to artist · M83" in texts and "Go to album · Junk" in texts
    sub = next(a.menu() for a in menu.actions() if a.menu())
    assert [a.text() for a in sub.actions() if a.text()] == \
        ["＋  New playlist…", "Kitchen", "Top List"]


def test_menu_for_radio_has_no_queue_or_playlist(app):
    b = _browser(app)
    texts = [a.text() for a in b.build_menu([_entry("stream")]).actions()]
    assert "▶  Play" in texts
    assert not any("queue" in t.lower() or "playlist" in t.lower() for t in texts)


def test_menu_for_several_songs_skips_navigation(app):
    b = _browser(app)
    texts = [a.text() for a in b.build_menu(
        [_entry("track", artist_id="a"), _entry("track", artist_id="b")]).actions()]
    assert "▶  Play" in texts and not any(t.startswith("Go to") for t in texts)


def test_card_titles_wrap_instead_of_hiding_the_difference(app):
    from PyQt6.QtGui import QFont, QFontMetrics

    from sonolin.gui.browser import _two_lines
    fm = QFontMetrics(QFont())
    width = fm.horizontalAdvance("Discover Weekly") + 4
    assert _two_lines(fm, "Discover Weekly", width) == ["Discover Weekly"]
    assert _two_lines(fm, "Discover Weekly October", width) == ["Discover Weekly", "October"]
    long = _two_lines(fm, "Discover Weekly October and November and December too", width)
    assert len(long) == 2 and long[1].endswith("…")


def test_menu_play_on_one_song_keeps_its_list(app, monkeypatch):
    b = _browser(app)
    album = [_entry("track", title=f"t{n}") for n in range(5)]
    seen = []
    monkeypatch.setattr(b, "play_from", lambda entries, row: seen.append((len(entries), row)))
    monkeypatch.setattr(b, "play", lambda entries: seen.append(("exact", len(entries))))
    menu = b.build_menu([album[3]], context=album)
    next(a for a in menu.actions() if a.text() == "▶  Play").trigger()
    menu = b.build_menu(album[1:3], context=album)
    next(a for a in menu.actions() if a.text() == "▶  Play").trigger()
    assert seen == [(5, 3), ("exact", 2)]


# -- artwork loading --------------------------------------------------------------

import time  # noqa: E402

from sonolin.gui.browser import ArtLoader, _Lane  # noqa: E402


def test_the_latest_request_goes_first_a_few_at_a_time():
    started = []
    lane = _Lane(2, started.append)
    for key in "abcde":
        lane.ask(key)
    assert started == ["a", "b"], "no more than two at once"
    lane.ask("c")                   # scrolled back to c: it moves up
    lane.ask("a")                   # already running: not started twice
    lane.done("a")
    assert started == ["a", "b", "c"]
    lane.done("b")
    assert started[-1] == "e"
    lane.done("c"); lane.done("e")
    assert started[-1] == "d" and not lane.waiting


def _png(path, colour="teal"):
    img = QImage(64, 64, QImage.Format.Format_RGB32)
    img.fill(QColor(colour))
    img.save(str(path))
    return path.as_uri()


def _wait(app, done, seconds=5.0):
    end = time.monotonic() + seconds
    while not done() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)
    return done()


SPEAKER_ART = "http://speaker.invalid:1400/getaa?s=1&u=song{}"


def _loader(monkeypatch):
    """A loader that records what it would ask a speaker for, without asking."""
    loader = ArtLoader()
    sent, real_send = [], loader._send
    monkeypatch.setattr(loader, "_send", lambda url: sent.append(url) if url.startswith(
        "http") else real_send(url))
    loader._speaker_lane.start = loader._send
    landed = []
    loader.ready.connect(landed.append)
    return loader, sent, landed


def test_a_resolved_cover_comes_from_the_faster_address(app, tmp_path, monkeypatch):
    loader, sent, landed = _loader(monkeypatch)
    fast = _png(tmp_path / "cover.png")
    loader.resolver = lambda url: (lambda: fast)
    assert loader.pixmap(SPEAKER_ART.format(1), 40) is None
    assert _wait(app, lambda: SPEAKER_ART.format(1) in landed)
    assert loader.pixmap(SPEAKER_ART.format(1), 40) is not None
    assert sent == [], "the speaker was never asked"


def test_songs_resolving_to_one_picture_fetch_it_once(app, tmp_path, monkeypatch):
    loader, sent, landed = _loader(monkeypatch)
    fast = _png(tmp_path / "album.png")
    fetched = []
    real_send = loader._send
    monkeypatch.setattr(loader, "_send", lambda url: (fetched.append(url), real_send(url)))
    loader._speaker_lane.start = loader._send
    loader.resolver = lambda url: (lambda: fast)
    for n in range(3):
        loader.pixmap(SPEAKER_ART.format(n), 40)
    assert _wait(app, lambda: all(SPEAKER_ART.format(n) in landed for n in range(3)))
    assert fetched == [fast]


def test_a_failed_lookup_falls_back_to_the_speaker(app, tmp_path, monkeypatch):
    loader, sent, landed = _loader(monkeypatch)
    loader.resolver = lambda url: (lambda: (_ for _ in ()).throw(OSError("no route")))
    loader.pixmap(SPEAKER_ART.format(1), 40)
    assert _wait(app, lambda: sent == [SPEAKER_ART.format(1)])


def test_a_broken_faster_address_falls_back_to_the_speaker(app, tmp_path, monkeypatch):
    loader, sent, landed = _loader(monkeypatch)
    missing = (tmp_path / "gone.png").as_uri()
    loader.resolver = lambda url: (lambda: missing)
    loader.pixmap(SPEAKER_ART.format(1), 40)
    assert _wait(app, lambda: sent == [SPEAKER_ART.format(1)])


def test_other_covers_are_fetched_directly(app, tmp_path, monkeypatch):
    loader, sent, landed = _loader(monkeypatch)
    loader.resolver = lambda url: None
    loader.pixmap("https://i.scdn.co/image/abc", 40)
    assert sent == ["https://i.scdn.co/image/abc"]


def test_a_speaker_is_asked_for_two_covers_at_a_time(app, monkeypatch):
    loader, sent, landed = _loader(monkeypatch)
    for n in range(6):
        loader.pixmap(SPEAKER_ART.format(n), 40)
    assert sent == [], "nothing starts until the whole screenful is known"
    app.processEvents()
    assert sent == [SPEAKER_ART.format(5), SPEAKER_ART.format(4)], "newest first, two at once"
    assert len(loader._speaker_lane.waiting) == 4


def test_starting_waits_for_the_whole_batch():
    started, deferred = [], []
    lane = _Lane(2, started.append, deferred.append)
    for key in "abcd":
        lane.ask(key)
    assert started == [] and len(deferred) == 1, "one start is scheduled, not four"
    deferred.pop()()
    assert started == ["d", "c"]


def test_a_resolved_cover_is_remembered_for_next_time(app, tmp_path, monkeypatch):
    loader, sent, landed = _loader(monkeypatch)
    fast = _png(tmp_path / "cover.png")
    loader.resolver = lambda url: (lambda: fast)
    loader.pixmap(SPEAKER_ART.format(1), 40)
    assert _wait(app, lambda: SPEAKER_ART.format(1) in landed)
    assert loader._save_soon.isActive()
    loader._save_sources()          # as the timer would

    again, sent, landed = _loader(monkeypatch)
    again.resolver = lambda url: pytest.fail("looked up again")
    again.pixmap(SPEAKER_ART.format(1), 40)
    assert _wait(app, lambda: SPEAKER_ART.format(1) in landed)
    assert sent == []


def test_a_damaged_address_file_is_ignored(app, monkeypatch):
    loader, sent, landed = _loader(monkeypatch)
    loader._sources_file.write_text("{not json")
    assert ArtLoader()._load_sources() == {}
