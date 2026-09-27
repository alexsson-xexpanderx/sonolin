import xml.etree.ElementTree as ET
from types import SimpleNamespace

from sonolin.services import BROADCAST_PREFIX, Services, ServiceInfo


def item(kind, **meta):
    return SimpleNamespace(title="Jazz24 & Co <HD2>", desc="SA_RINCON65031_",
                           metadata={"id": "s34682", "item_type": kind, **meta})


def test_kinds():
    assert Services.is_stream(item("stream"))
    assert not Services.is_stream(item("track"))
    assert Services.is_container(item("album"))
    assert Services.is_container(item("container"))
    assert Services.is_container(item("thing", can_enumerate="true"))
    assert not Services.is_container(item("track"))


def test_stream_uri_and_metadata(monkeypatch):
    svc = Services()
    fake = SimpleNamespace(service_id=254, service_type=65031, account=None)
    monkeypatch.setattr(svc, "open", lambda name, speaker: fake)
    uri, meta = svc.stream_uri_and_meta("TuneIn", None, item("stream"))
    assert uri == "x-sonosapi-stream:s34682?sid=254&flags=8224&sn=0"
    root = ET.fromstring(meta)
    ns = {"d": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
          "dc": "http://purl.org/dc/elements/1.1/",
          "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/"}
    it = root.find("d:item", ns)
    assert it.get("id") == BROADCAST_PREFIX + "s34682"
    assert it.find("dc:title", ns).text == "Jazz24 & Co <HD2>"
    assert it.find("upnp:class", ns).text == "object.item.audioItem.audioBroadcast"
    assert it.find("d:desc", ns).text == "SA_RINCON65031_"


def test_linked_account_serial_is_used(monkeypatch):
    svc = Services()
    fake = SimpleNamespace(service_id=303, service_type=77575,
                           account=SimpleNamespace(serial_number=4))
    monkeypatch.setattr(svc, "open", lambda name, speaker: fake)
    uri, _ = svc.stream_uri_and_meta("TuneIn (New)", None, item("stream"))
    assert uri.endswith("sid=303&flags=8224&sn=4")


def test_play_routes_radio_and_queueable_items(monkeypatch):
    calls = []
    soco = SimpleNamespace(
        play_uri=lambda uri, meta, title, start: calls.append(("play_uri", uri, start)),
        add_to_queue=lambda it: calls.append(("queue", it.title)) or 7,
        play_from_queue=lambda i: calls.append(("from_queue", i)),
    )
    speaker = SimpleNamespace(soco=soco)
    svc = Services()
    monkeypatch.setattr(svc, "open", lambda n, s: SimpleNamespace(
        service_id=254, service_type=65031, account=None))
    svc.play("TuneIn", speaker, item("stream"), start=False)
    svc.play("Spotify", speaker, item("track"))
    assert calls[0][0] == "play_uri" and calls[0][2] is False
    assert calls[1:] == [("queue", "Jazz24 & Co <HD2>"), ("from_queue", 6)]


def test_link_requirement():
    assert ServiceInfo("Spotify", "AppLink", 9, 2311).needs_link
    assert ServiceInfo("Sonos Radio", "DeviceLink", 1, 1).needs_link
    assert not ServiceInfo("TuneIn", "Anonymous", 254, 65031).needs_link


# -- describe(): one view of SMAPI's differently shaped items ---------------

def _ms_item(title, **meta):
    tm = meta.pop("track", None)
    if tm is not None:
        meta["track_metadata"] = SimpleNamespace(metadata=tm)
    return SimpleNamespace(title=title, metadata=meta)


def test_describe_track_gathers_track_metadata():
    from sonolin.services import describe
    e = describe(_ms_item("Midnight City", id="spotify:track:X", item_type="track", track={
        "artist": "M83", "album": "Hurry up", "duration": "243", "track_number": "2",
        "album_art_uri": "https://i/art", "artist_id": "spotify:artist:A",
        "album_id": "spotify:album:B", "can_play": "True"}))
    assert (e.kind, e.subtitle, e.art, e.duration, e.number) == \
        ("track", "M83 · Hurry up", "https://i/art", 243.0, 2)
    assert (e.artist_id, e.album_id) == ("spotify:artist:A", "spotify:album:B")
    assert e.playable and e.queueable and not e.openable


def test_describe_kinds():
    from sonolin.services import describe
    artist = describe(_ms_item("M83", item_type="artist", can_play="False", can_enumerate="True"))
    album = describe(_ms_item("Junk", item_type="album", artist="M83", can_play="True"))
    playlist = describe(_ms_item("Chill", item_type="playlist", artist="Spotify"))
    radio = describe(_ms_item("Jazz24", item_type="stream", genre="Jazz"))
    folder = describe(_ms_item("Charts", item_type="collection"))
    assert artist.openable and not artist.playable and artist.subtitle == "Artist"
    assert album.subtitle == "M83" and album.queueable
    assert playlist.subtitle == "by Spotify"
    assert radio.playable and not radio.queueable and radio.subtitle == "Jazz"
    assert folder.kind == "folder" and folder.openable


def test_bad_numbers_do_not_raise():
    from sonolin.services import describe
    e = describe(_ms_item("x", item_type="track", track={"duration": "n/a", "track_number": "?"}))
    assert (e.duration, e.number) == (0.0, 0)


def test_single_result_is_wrapped_before_soco_parses_it(monkeypatch):
    import soco.music_services.data_structures as ds

    seen = {}
    monkeypatch.setattr(ds, "parse_response",
                        lambda service, response, label: seen.setdefault("r", response) and [])
    one = {"id": "x", "itemType": "artist", "title": "M83"}
    service = SimpleNamespace(soap_client=SimpleNamespace(
        call=lambda method, args: {"searchResult": {"count": "1", "mediaCollection": one}}))
    Services._query(service, "search", [], "artists")
    assert seen["r"]["searchResult"]["mediaCollection"] == [one]


def test_play_next_inserts_after_the_current_track_in_order():
    calls = []
    soco = SimpleNamespace(
        get_current_track_info=lambda: {"playlist_position": "4"},
        add_to_queue=lambda item, position, as_next: calls.append((item.title, position, as_next)) or position,
    )
    items = [_ms_item("a", item_type="track"), _ms_item("radio", item_type="stream"),
             _ms_item("b", item_type="album")]
    first = Services.enqueue(SimpleNamespace(soco=soco), items, next_up=True)
    assert calls == [("a", 5, True), ("b", 6, True)], "radio must be skipped"
    assert first == 5


def test_add_to_end_of_queue():
    calls = []
    soco = SimpleNamespace(add_to_queue=lambda item, position, as_next: calls.append(position) or 11)
    Services.enqueue(SimpleNamespace(soco=soco), [_ms_item("a", item_type="track")])
    assert calls == [0]


def test_radio_never_goes_into_a_playlist():
    added = []
    soco = SimpleNamespace(add_item_to_sonos_playlist=lambda item, pl: added.append(item.title))
    n = Services.add_to_playlist(SimpleNamespace(soco=soco), "PL",
                                 [_ms_item("a", item_type="track"), _ms_item("r", item_type="stream")])
    assert (n, added) == (1, ["a"])


def test_personal_playlists_are_told_apart_from_editorial_ones():
    from sonolin.services import describe, is_personal

    def make(pid):
        return describe(_ms_item("x", item_type="playlist", id=f"spotify:playlist:{pid}"))

    assert is_personal(make("37i9dQZEVXcI9HgyEbN2YS"))       # Discover Weekly
    assert is_personal(make("37i9dQZF1E392zprm6C95X"))       # Daily Mix
    assert not is_personal(make("37i9dQZF1DX4UtSsGT1Sbe"))   # All Out 80s, editorial
    assert not is_personal(make("7pOGVa89onKv8P5013UjRl"))   # made by the user


def test_home_page_opens_the_library_folder():
    folders = {
        "root": [_ms_item("Charts", item_type="collection"),
                 _ms_item("Your Music", item_type="collection")],
        "Your Music": [_ms_item("Playlists", item_type="container"),
                       _ms_item("Albums", item_type="container")],
        "Playlists": [_ms_item("Discover Weekly", item_type="playlist")],
        "Albums": [_ms_item("Junk", item_type="album")],
    }
    svc = Services()
    svc.browse = lambda name, sp, item=None, count=100: folders["root" if item is None else item.title]
    root, sections = svc.home_page("Spotify", None)
    assert [i.title for i in root] == ["Charts", "Your Music"]
    assert [(f.title, [i.title for i in items]) for f, items in sections] == \
        [("Playlists", ["Discover Weekly"]), ("Albums", ["Junk"])]


# -- "Play" continues down the list it was picked from ------------------------

def _recording_soco():
    calls = []
    soco = SimpleNamespace(
        clear_queue=lambda: calls.append(("clear",)),
        add_multiple_to_queue=lambda items: calls.append(("add_many", [i.title for i in items])),
        add_to_queue=lambda item: calls.append(("add", item.title)) or 1,
        play_from_queue=lambda i: calls.append(("play_from", i)),
        play_uri=lambda uri, meta, title, start: calls.append(("play_uri", title)),
    )
    return soco, calls


def test_a_song_plays_on_down_its_album():
    soco, calls = _recording_soco()
    album = [_ms_item(f"t{n}", item_type="track") for n in range(1, 11)]
    Services().play_context("Spotify", SimpleNamespace(soco=soco), album, start=3)
    assert calls == [("clear",), ("add_many", [f"t{n}" for n in range(1, 11)]),
                     ("play_from", 3)]


def test_an_album_plays_from_the_top():
    soco, calls = _recording_soco()
    Services().play_context("Spotify", SimpleNamespace(soco=soco),
                            [_ms_item("Crystal City", item_type="album")])
    assert calls == [("clear",), ("add", "Crystal City"), ("play_from", 0)]


def test_radio_plays_without_touching_the_queue(monkeypatch):
    soco, calls = _recording_soco()
    svc = Services()
    monkeypatch.setattr(svc, "open", lambda n, s: SimpleNamespace(
        service_id=254, service_type=65031, account=None))
    radio = _ms_item("Jazz24", item_type="stream", id="s34682")
    radio.desc = "SA_RINCON65031_"
    svc.play_context("TuneIn", SimpleNamespace(soco=soco), [radio])
    assert calls == [("play_uri", "Jazz24")]



def test_token_file_is_private(tmp_path, monkeypatch):
    import stat

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from sonolin.services import token_path
    Services().store
    assert stat.S_IMODE(token_path().stat().st_mode) == 0o600
    token_path().chmod(0o644)       # as SoCo used to leave it
    Services().store
    assert stat.S_IMODE(token_path().stat().st_mode) == 0o600
