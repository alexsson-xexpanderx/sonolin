"""MPRIS state handling and wire types, without registering on a bus."""

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QCoreApplication, QMetaType  # noqa: E402
from PyQt6.QtDBus import QDBusObjectPath  # noqa: E402

from nosonpy.gui.mpris import NO_TRACK, Mpris  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QCoreApplication.instance() or QCoreApplication([])


def test_initial_state(app):
    m = Mpris()
    assert m.status == "Stopped" and m.track_id == NO_TRACK
    meta = m.metadata()
    assert isinstance(meta["mpris:trackid"], QDBusObjectPath)
    assert "mpris:length" not in meta


def test_update_maps_sonos_state_and_types(app):
    m = Mpris()
    m.update(state="PLAYING", title="T", artist="A", album="B", length=243.5,
             position=10, volume=30, shuffle=True, repeat="ONE")
    assert m.status == "Playing" and m.loop == "Track" and m.shuffle is True
    assert m.volume == pytest.approx(0.30)
    length = m.metadata()["mpris:length"]
    assert length.metaType().id() == QMetaType.Type.LongLong.value
    assert length.value() == 243_500_000


def test_new_track_gets_a_new_id(app):
    m = Mpris()
    m.update(state="PLAYING", title="One", length=100)
    first = m.track_id
    m.update(state="PLAYING", title="One", length=100)
    assert m.track_id == first
    m.update(state="PLAYING", title="Two", length=100)
    assert m.track_id != first


def test_transitioning_counts_as_playing_and_advance_is_bounded(app):
    m = Mpris()
    m.update(state="TRANSITIONING", title="x", length=5, position=4)
    assert m.status == "Playing"
    m.advance(10)
    assert m.position == 5


def test_commands_are_emitted(app):
    m = Mpris()
    got = []
    m.command.connect(lambda name, arg: got.append((name, arg)))
    m.update(state="PLAYING", title="x", length=100, position=20)
    m._player.PlayPause()
    m._player.Seek(30_000_000)        # +30 s
    m._player.Seek(-60_000_000)       # clamps at 0
    m._player.Seek(500_000_000)       # past the end: the spec says skip
    m._player.SetPosition(QDBusObjectPath("/wrong/track"), 5_000_000)  # ignored
    m._player.SetPosition(QDBusObjectPath(m.track_id), 5_000_000)
    assert got == [("playpause", None), ("seek", 50.0), ("seek", 0.0),
                   ("next", None), ("seek", 5.0)]
