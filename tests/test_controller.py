import datetime
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from sonolin.controller import Controller
from sonolin.mediaserver import MediaServer
from sonolin.speaker import _Loop
from sonolin.tags import Tags


@pytest.fixture
def controller(xdg, music_dir):
    c = Controller([music_dir])
    c.library.scan()
    c.server = MediaServer(c.library, host="127.0.0.1")
    _Loop.submit(c.server.start())
    yield c
    c.close()


def test_didl_is_well_formed_and_escaped(controller):
    t = Tags(path=Path("/music/x.flac"), title='Rock & Roll <Live> "2"', artist="AC/DC & co",
             album="Q&A", duration=125.4)
    didl = controller.didl_for(t)
    root = ET.fromstring(didl)  # raises if the escaping is wrong
    ns = {"dc": "http://purl.org/dc/elements/1.1/",
          "d": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
          "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/"}
    assert root.find(".//dc:title", ns).text == 'Rock & Roll <Live> "2"'
    assert root.find(".//upnp:album", ns).text == "Q&A"
    res = root.find(".//d:res", ns)
    assert res.get("protocolInfo") == "http-get:*:audio/flac:*"
    assert res.get("duration") == "0:02:05"
    assert res.text.endswith(".flac")
    assert root.find(".//upnp:albumArtURI", ns).text.startswith(controller.server_url)


def test_stream_schemes_are_recognised():
    assert "x-sonosapi-stream:s1234?sid=254".startswith(Controller.STREAM_SCHEMES)
    assert not "x-rincon-cpcontainer:1004206c".startswith(Controller.STREAM_SCHEMES)


def test_bad_recurrence_is_refused_before_any_network(xdg):
    c = Controller([])
    with pytest.raises(ValueError):
        c.create_alarm(SimpleNamespace(soco=None), datetime.time(7), recurrence="SOMETIMES")


def test_music_dirs_are_remembered(xdg, music_dir):
    c = Controller([music_dir])
    c._save()
    assert Controller().library.roots == [music_dir]


def test_local_songs_play_on_down_the_list(xdg, monkeypatch):
    c = Controller([])
    calls = []
    speaker = SimpleNamespace(soco=SimpleNamespace(
        clear_queue=lambda: calls.append("clear"),
        play_from_queue=lambda i: calls.append(("play_from", i))))
    monkeypatch.setattr(c, "enqueue", lambda sp, tracks: calls.append(("enqueue", len(tracks))))
    c.play_local_list(speaker, [Tags(path=Path(f"/m/{n}.flac")) for n in range(5)], start=2)
    assert calls == ["clear", ("enqueue", 5), ("play_from", 2)]


def test_desktop_session_is_created_and_revoked(controller):
    from unittest.mock import Mock

    speaker = SimpleNamespace(soco=Mock())
    url = controller.stream_desktop(speaker, "mp3")
    assert "/stream/live.mp3?token=" in url
    speaker.soco.play_uri.assert_called_once_with(url, title="Desktop audio")
    controller.stop_desktop()
    assert controller.server._live_token is None


def test_desktop_playback_failure_revokes_session(controller):
    from unittest.mock import Mock

    speaker = SimpleNamespace(soco=Mock())
    speaker.soco.play_uri.side_effect = OSError("speaker unreachable")
    with pytest.raises(OSError):
        controller.stream_desktop(speaker)
    assert controller.server._live_token is None


def test_removed_tracks_are_remembered_across_runs(xdg, music_dir):
    from sonolin.config import Config

    c = Controller([music_dir], config=Config()); c.library.scan()
    c.hide_tracks([t for t in c.library.tracks if t.path.name == "b.mp3"])
    again = Controller([music_dir], config=Config.load())
    assert again.library.scan() == 3


@pytest.mark.parametrize("uri, expected", [
    ("x-sonos-spotify:spotify%3atrack%3a1l5L?sid=9&flags=8232&sn=2", "Spotify (by number)"),
    ("x-sonos-spotify:spotify%3atrack%3a1l5L?flags=8232", "Spotify"),
    ("x-sonosapi-stream:s24861?sid=254&flags=8224", "Radio"),
    ("x-rincon-stream:RINCON_000", "Line-in"),
    ("x-sonos-htastream:RINCON_000:spdif", "TV"),
    ("http://192.168.1.2:1405/stream/live.flac", "This computer"),
    ("https://cdn.example/clip/1.mp3", "Web link"),
    ("x-sonos-http:song%3a1.mp4?sid=204&flags=8224", "A music service"),
    ("", ""),
])
def test_where_a_song_comes_from_in_plain_words(uri, expected):
    from sonolin.controller import source_of

    names = {9: "Spotify (by number)"}
    assert source_of(uri, lambda sid: names.get(sid, "")) == expected


def test_a_library_song_is_found_from_its_address(xdg, music_dir):
    from sonolin.config import Config
    from sonolin.controller import describe_format
    from sonolin.mediaserver import token_for

    c = Controller([music_dir], config=Config()); c.library.scan()
    song = next(t for t in c.library.tracks if t.path.suffix == ".flac")
    uri = f"http://10.0.0.5:1405/music/{token_for(song.path)}.flac"
    assert c.local_track(uri) is song
    assert c.local_track("http://10.0.0.5:1405/music/" + "0" * 20 + ".flac") is None
    assert c.local_track("x-sonos-spotify:spotify%3atrack%3a1?sid=9") is None
    assert describe_format(song) == "FLAC · 44.1 kHz · stereo"
    assert describe_format(Tags(Path("a.mp3"), channels=1, sample_rate=48000)) == \
        "MP3 · 48 kHz · mono"
