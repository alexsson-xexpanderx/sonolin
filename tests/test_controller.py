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
