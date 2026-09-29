"""The queue's now-playing mark, without a speaker."""

from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from sonolin.gui.panels import QueueTab  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _items(*ids):
    return [SimpleNamespace(title=f"song {i}", creator="M83",
                            resources=[SimpleNamespace(uri=f"x-sonos-spotify:spotify%3atrack%3a{i}?sid=9&flags=8232&sn=2")])
            for i in ids]


def _uri(i, flags="8232"):
    return f"x-sonos-spotify:spotify%3atrack%3a{i}?sid=9&flags={flags}&sn=2"


def _cell(q, row, col=0):
    return q.table.item(row, col).text()


def test_marks_the_song_at_the_reported_position(app):
    q = QueueTab(); q.load(_items("A", "B", "C"))
    q.set_playing(2, _uri("B"), "PLAYING")
    assert q.playing_row == 1 and _cell(q, 1) == "2", "no play or pause sign"
    assert q.table.item(1, 1).font().bold()
    q.set_playing(2, _uri("B"), "PAUSED_PLAYBACK")
    assert q.playing_row == 1 and _cell(q, 1) == "2"


def test_moving_on_restores_the_previous_row(app):
    q = QueueTab(); q.load(_items("A", "B", "C"))
    q.set_playing(1, _uri("A"), "PLAYING")
    q.set_playing(2, _uri("B"), "PLAYING")
    assert _cell(q, 0) == "1" and not q.table.item(0, 1).font().bold()
    assert q.playing_row == 1


def test_no_mark_when_the_queue_is_not_what_is_playing(app):
    q = QueueTab(); q.load(_items("A", "B"))
    q.set_playing(1, "x-sonosapi-stream:s34682?sid=254", "PLAYING")  # radio
    assert q.playing_row == -1
    q.set_playing(5, _uri("A"), "PLAYING")                          # stale position
    assert q.playing_row == -1


def test_flags_written_differently_still_match(app):
    q = QueueTab(); q.load(_items("A"))
    q.set_playing(1, _uri("A", flags="8224"), "PLAYING")
    assert q.playing_row == 0


def test_reload_keeps_the_mark(app):
    q = QueueTab(); q.load(_items("A", "B"))
    q.set_playing(2, _uri("B"), "PLAYING")
    q.load(_items("A", "B", "C"))
    assert q.playing_row == 1 and q.table.item(1, 1).font().bold()



def test_every_table_is_left_aligned(app):
    from PyQt6.QtCore import Qt

    from sonolin.gui.panels import AlarmsTab, LibraryTab

    q = QueueTab(); q.load(_items("A", "B"))
    q.set_playing(1, _uri("A"), "PLAYING")
    library, alarms = LibraryTab(), AlarmsTab()  # keep the tabs alive: they own the tables
    for table in (q.table, library.tracks, alarms.table):
        assert table.horizontalHeader().defaultAlignment() & Qt.AlignmentFlag.AlignLeft
    for row in range(2):
        for col in range(3):
            align = q.table.item(row, col).textAlignment()
            assert not align & Qt.AlignmentFlag.AlignHCenter, (row, col)


# -- covers ---------------------------------------------------------------------

from PyQt6.QtCore import QObject, pyqtSignal  # noqa: E402
from PyQt6.QtGui import QPixmap  # noqa: E402


class FakeLoader(QObject):
    ready = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.asked = []
        self.have = {}

    def pixmap(self, url, size, round_=False):
        self.asked.append(url)
        return self.have.get(url)

    def placeholder(self, entry, size, round_=False):
        pix = QPixmap(size, size)
        pix.fill()
        return pix


def _long_queue(n):
    items = _items(*[f"T{i}" for i in range(n)])
    return items, [f"http://speaker:1400/getaa?t={i}" for i in range(n)]


def test_only_covers_near_the_screen_are_requested(app):
    q = QueueTab()
    loader = FakeLoader()
    q.set_art_loader(loader)
    q.resize(600, 300)
    q.show()
    q.load(*_long_queue(500))
    app.processEvents()
    first = set(loader.asked)
    assert 0 < len(first) < 40, f"asked for {len(first)} covers up front"
    assert "http://speaker:1400/getaa?t=0" in first
    q.table.verticalScrollBar().setValue(q.table.verticalScrollBar().maximum())
    app.processEvents()
    assert "http://speaker:1400/getaa?t=499" in loader.asked
    assert len(set(loader.asked)) < 90, "scrolling fetches what comes into view, not everything"


def test_a_cover_that_arrives_later_is_shown(app):
    q = QueueTab()
    loader = FakeLoader()
    q.set_art_loader(loader)
    q.resize(600, 300)
    q.show()
    items, arts = _long_queue(3)
    q.load(items, arts)
    app.processEvents()
    placeholder_key = q.table.item(1, 1).icon().cacheKey()
    cover = QPixmap(40, 40)
    cover.fill()
    loader.have[arts[1]] = cover
    loader.ready.emit(arts[1])
    assert q.table.item(1, 1).icon().cacheKey() != placeholder_key


def test_entries_without_a_cover_keep_the_tile(app):
    q = QueueTab()
    loader = FakeLoader()
    q.set_art_loader(loader)
    q.show()
    q.load(_items("A"), [""])
    app.processEvents()
    assert not q.table.item(0, 1).icon().isNull()
    assert loader.asked == [], "nothing to fetch for an entry with no cover"


def test_songs_from_one_album_share_one_cover(app):
    q = QueueTab()
    loader = FakeLoader()
    q.set_art_loader(loader)
    q.resize(600, 300)
    q.show()
    items, arts = _long_queue(4)
    for item, album, artist in zip(items, ["Hits", "Hits", "Hits", "Hits"],
                                   ["Elvis", "Elvis", "Elvis", "Abba"]):
        item.album, item.creator = album, artist
    q.load(items, arts)
    app.processEvents()
    assert set(loader.asked) == {arts[0], arts[3]}, \
        "one cover per album, and another artist's Hits is another album"


def test_the_top_of_the_screen_is_asked_for_last(app):
    """The loader serves the latest request first."""
    q = QueueTab()
    loader = FakeLoader()
    q.set_art_loader(loader)
    q.resize(600, 300)
    q.show()
    items, arts = _long_queue(100)
    q.load(items, arts)
    app.processEvents()
    assert loader.asked[-1] == arts[0]
    below = q.table.rowAt(q.table.viewport().height() - 1) + 1
    assert loader.asked.index(arts[below]) < loader.asked.index(arts[1]), \
        "rows just off screen go before the visible ones"


def test_the_scroll_bar_starts_below_the_headings(app):
    from PyQt6.QtCore import QPoint

    q = QueueTab()
    q.load(*_long_queue(60))
    q.resize(600, 300)
    q.show()
    app.processEvents()
    bar = q.table.verticalScrollBar()
    assert bar.isVisible()
    top = bar.mapTo(q.table, QPoint(0, 0)).y()
    assert top >= q.table.horizontalHeader().height(), "the bar must not run up beside the headings"


def test_a_click_on_the_bar_goes_straight_there(app):
    from PyQt6.QtCore import QPoint, Qt
    from PyQt6.QtTest import QTest

    from sonolin.gui.panels import JumpSlider

    s = JumpSlider(Qt.Orientation.Horizontal)
    s.setRange(0, 100); s.resize(220, 24); s.show()
    try:
        QTest.mouseClick(s, Qt.MouseButton.LeftButton, pos=QPoint(165, 12))
        assert 65 <= s.value() <= 85, "not one page step (10) towards the click"
        QTest.mouseClick(s, Qt.MouseButton.LeftButton, pos=QPoint(3, 12))
        assert s.value() == 0
    finally:
        s.close()
