"""Finding the song behind a web link, without the network."""

from types import SimpleNamespace

import pytest

from sonolin.controller import Controller
from sonolin.links import PRIVATE, Link
from sonolin.links import resolve as _resolve

MP3 = b"ID3\x04\x00" + bytes(11)
#: The start of a Suno private-song file: scrambled, no format a speaker reads.
SCRAMBLED = bytes.fromhex("bb2adede8af0cc3733d2c7078693 4c78".replace(" ", ""))


def resolve(address, fetch, start=MP3):
    return _resolve(address, fetch=fetch, head=lambda url: start)


def page(html: str, final: str = "https://example.com/song/1", ctype="text/html; charset=utf-8"):
    def fetch(url):
        fetch.asked.append(url)
        return final, ctype, html.encode()
    fetch.asked = []
    return fetch


def test_a_sound_file_address_is_played_as_it_is():
    fetch = page("")
    link = resolve(" https://example.com/music/My%20Song.m4a ", fetch=fetch)
    assert link == Link("https://example.com/music/My%20Song.m4a", "My Song", mime="audio/mp4")
    assert fetch.asked == []  # the speaker fetches it; nothing to read here


def test_a_page_names_its_song_for_link_previews():
    fetch = page('<html><head><title>Ignored</title>'
                 '<meta property="og:title" content="Night &amp; Day"/>'
                 '<meta property="og:image" content="/cover.jpg"/>'
                 '<meta property="og:audio" content="/files/night.mp3"/></head></html>')
    link = resolve("https://example.com/s/abc", fetch=fetch)
    assert link.url == "https://example.com/files/night.mp3"
    assert link.title == "Night & Day"
    assert link.art == "https://example.com/cover.jpg"
    assert link.mime == "audio/mpeg"


def suno(audio_url: str):
    data = (rf'\"audio_url\":\"{audio_url}\",'
            r'\"media_urls\":[{\"url\":\"https://cdn.example/1/clip/0000-1111.m4a\"}]')
    return page('<meta property="og:title" content="A Song"/>'
                '<script>x("https://cdn.example/sil-100.mp3")</script>'
                f'<script>self.__next_f.push([1,"{data}"])</script>')


def test_a_public_suno_song_plays_its_mp3():
    link = resolve("https://suno.com/s/abc", fetch=suno("https://cdn1.example/0000-1111.mp3"))
    assert link.url == "https://cdn1.example/0000-1111.mp3"
    assert link.title == "A Song" and link.mime == "audio/mpeg"


def test_a_private_suno_song_says_so_instead_of_playing_its_scrambled_file():
    with pytest.raises(ValueError) as caught:
        resolve("https://suno.com/s/abc", fetch=suno("https://studio-api.example/api/forbidden"))
    assert str(caught.value) == PRIVATE


@pytest.mark.parametrize("start, ok", [
    (MP3, True), (b"\xff\xfb\x90\x64" + bytes(12), True),
    (b"\x00\x00\x00\x20ftypM4A " + bytes(4), True), (b"fLaC" + bytes(12), True),
    (b"OggS" + bytes(12), True), (SCRAMBLED, False), (b"<!DOCTYPE html>\n", False),
])
def test_only_a_real_sound_file_is_given_to_the_speaker(start, ok):
    play = lambda: resolve("https://example.com/a.m4a", fetch=page(""), start=start)  # noqa: E731
    if ok:
        assert play().url == "https://example.com/a.m4a"
    else:
        with pytest.raises(ValueError, match="does not lead to a sound file"):
            play()


def test_a_link_that_turns_out_to_be_a_sound_file():
    fetch = page("", final="https://example.com/stream/42", ctype="audio/ogg")
    link = resolve("https://example.com/play/42", fetch=fetch)
    assert link.url == "https://example.com/stream/42" and link.mime == "audio/ogg"


@pytest.mark.parametrize("address, fetch, words", [
    ("suno.com/s/abc", page(""), "not a web link"),
    ("file:///etc/passwd", page(""), "not a web link"),
    ("https://example.com/private", page("<title>Private</title>"), "No song found"),
    ("https://example.com/x", page('<meta property="og:audio" content="javascript:x"/>'),
     "No song found"),
])
def test_what_cannot_be_played_says_why_in_plain_words(address, fetch, words):
    with pytest.raises(ValueError, match=words):
        resolve(address, fetch=fetch)


def test_an_unreachable_link_is_a_plain_message():
    def fetch(url):
        raise OSError("timed out")
    with pytest.raises(ValueError, match="Could not open that link"):
        resolve("https://example.com/s/abc", fetch=fetch)


def test_a_queued_link_carries_its_title_and_kind():
    sent = []
    soco = SimpleNamespace(avTransport=SimpleNamespace(
        AddURIToQueue=lambda args: sent.append(dict(args)) or {"FirstTrackNumberEnqueued": "7"}))
    link = Link("https://cdn.example/clip/1.m4a", "Fish & <Chips>", art="https://cdn.example/a.jpg",
                mime="audio/mp4")
    assert Controller.queue_link(SimpleNamespace(soco=soco), link) == 7
    meta = sent[0]["EnqueuedURIMetaData"]
    assert sent[0]["EnqueuedURI"] == link.url
    assert "<dc:title>Fish &amp; &lt;Chips&gt;</dc:title>" in meta
    assert 'protocolInfo="http-get:*:audio/mp4:*">https://cdn.example/clip/1.m4a</res>' in meta
