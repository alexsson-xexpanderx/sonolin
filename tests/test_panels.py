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
    assert q.playing_row == 1 and _cell(q, 1) == "▶"
    assert q.table.item(1, 1).font().bold()
    q.set_playing(2, _uri("B"), "PAUSED_PLAYBACK")
    assert _cell(q, 1) == "❚❚"


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
    assert q.playing_row == 1 and _cell(q, 1) == "▶"
