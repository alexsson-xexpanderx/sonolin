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
