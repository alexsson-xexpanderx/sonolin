"""Music services: TuneIn, Spotify and the rest, browsed and searched through
the speaker's own service registry (SMAPI).

This replaces noson-app's `MediaModel`/`AllServicesModel` and its service
registration pages. SoCo does the SOAP; what it does not do is turn a search
result into something a speaker will actually play. Its DIDL for service items
is a placeholder that works for queueing tracks and containers but not for
radio, which cannot be queued at all and needs a real `x-sonosapi-stream:` URI
with a broadcast item. That translation is the job of `play`.

Services come in three kinds. Anonymous ones (legacy TuneIn) work straight
away. AppLink/DeviceLink ones (Spotify, TuneIn (New), Apple Music…) need this
app to be linked once: `begin_link` returns a web address the user opens and
logs in at, then `complete_link` collects the token. The login happens in the
user's browser with the service itself; no password passes through here.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .config import config_path

log = logging.getLogger(__name__)

#: DIDL item-id prefix Sonos itself uses for a broadcast from a service.
BROADCAST_PREFIX = "F00092020"


def token_path() -> Path:
    return config_path().parent / "service_tokens.json"


#: Guards the token store: its creation, and every save.
_TOKEN_LOCK = threading.Lock()


def _token_store(path: Path):
    """SoCo's JSON token store, made safe to share between threads.

    Cover lookups run several service calls at once, and any of them can come
    back with a refreshed token, which SoCo saves straight away. SoCo's own save
    empties the file and rewrites it in place, so two saves at once can leave it
    half-written and the service unlinked. Here saves take turns, and each one
    lands whole: the new file is written beside the old, private from the start,
    then renamed over it. The rename replaces the file a symlink points at, not
    the link.
    """
    from soco.music_services.token_store import JsonFileTokenStore

    class PrivateTokenStore(JsonFileTokenStore):
        def save_token_pair(self, music_service_id, household_id, token_pair):
            with _TOKEN_LOCK:
                super().save_token_pair(music_service_id, household_id, token_pair)

        def save_collection(self):
            target = Path(os.path.realpath(self.filepath))
            partial = target.with_name(target.name + ".partial")
            with contextlib.suppress(FileNotFoundError):
                partial.unlink()
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="UTF-8") as fh:
                json.dump(self._token_store, fh, indent=4)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(partial, target)

    return PrivateTokenStore(str(path), token_collection="sonolin")


#: Audio extensions some services append to the item id in a song's URI.
_MEDIA_SUFFIXES = (".mp3", ".mp4", ".m4a", ".flac", ".aac", ".ogg")


def service_track(cover_url: str) -> tuple[int, str] | None:
    """The service and item id behind a speaker's cover address, if it has one.

    A queued song's cover is ``http://<speaker>:1400/getaa?s=1&u=<song URI>``,
    and the URI of a song from a service names both the service and the song:
    ``x-sonos-spotify:spotify%3atrack%3a1l5L…?sid=9&flags=8232&sn=2``.
    """
    parts = urlsplit(cover_url)
    if not parts.path.endswith("/getaa"):
        return None
    uri = parse_qs(parts.query).get("u", [""])[0]
    scheme, _, rest = uri.partition(":")
    body, _, query = rest.partition("?")
    sid = parse_qs(query).get("sid", [""])[0]
    if not (scheme and body and sid.isdigit()):
        return None
    item_id = unquote(body)
    if scheme == "x-sonos-http" and item_id.lower().endswith(_MEDIA_SUFFIXES):
        item_id = item_id.rsplit(".", 1)[0]
    return int(sid), item_id


def art_address(metadata) -> str:
    """The picture in a service's ``getMediaMetadata`` answer, or ""."""
    for part, key in (("trackMetadata", "albumArtURI"), ("streamMetadata", "logo"),
                      (None, "albumArtURI")):
        holder = metadata.get(part) if part else metadata
        value = holder.get(key) if isinstance(holder, dict) else None
        if isinstance(value, dict):  # an address with attributes
            value = value.get("#text")
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return ""


#: SMAPI item types that are folders of other things rather than content.
FOLDER_TYPES = {
    "container", "collection", "favorites", "search", "albumList", "trackList",
    "artistTrackList", "otherContainer", "genre",
}


@dataclass
class Entry:
    """A search or browse result, described for display.

    SMAPI spreads what a UI needs across the item, its `track_metadata` and
    its `stream_metadata`, with different field sets per kind. This gathers it
    once, so views never have to know SMAPI's shape.
    """

    item: object
    kind: str          # artist, album, playlist, track, stream, program, folder
    title: str
    subtitle: str = ""
    art: str = ""
    duration: float = 0.0
    artist: str = ""
    album: str = ""
    artist_id: str = ""
    album_id: str = ""
    number: int = 0
    id: str = ""
    openable: bool = False
    playable: bool = False

    @property
    def queueable(self) -> bool:
        """Radio has no queue position; everything else playable does."""
        return self.playable and self.kind != "stream"


def describe(item) -> Entry:
    md = dict(getattr(item, "metadata", {}) or {})
    track = md.get("track_metadata")
    track = dict(getattr(track, "metadata", {}) or {}) if track is not None else {}
    stream = md.get("stream_metadata")
    stream = dict(getattr(stream, "metadata", {}) or {}) if stream is not None else {}
    raw = md.get("item_type", "")
    kind = raw if raw in ("artist", "album", "playlist", "track", "stream", "program") \
        else "folder"
    title = getattr(item, "title", "") or md.get("title", "") or "?"
    artist = track.get("artist") or md.get("artist") or ""
    album = track.get("album") or ""
    try:
        duration = float(track.get("duration") or 0)
    except ValueError:
        duration = 0.0
    try:
        number = int(track.get("track_number") or 0)
    except ValueError:
        number = 0
    subtitle = {
        "artist": "Artist",
        "album": artist,
        "playlist": f"by {artist}" if artist else "Playlist",
        "track": " · ".join(x for x in (artist, album) if x),
        "program": "Radio",
        "stream": md.get("genre") or stream.get("current_show") or "Radio station",
        "folder": "",
    }[kind]
    can_enum = str(md.get("can_enumerate", "")).lower() == "true"
    can_play = str(md.get("can_play", track.get("can_play", ""))).lower() == "true"
    return Entry(
        item=item, kind=kind, title=title, subtitle=subtitle,
        art=md.get("album_art_uri") or track.get("album_art_uri") or "",
        duration=duration, artist=artist, album=album,
        artist_id=track.get("artist_id") or md.get("artist_id") or "",
        album_id=track.get("album_id") or "", number=number, id=md.get("id", ""),
        openable=can_enum or kind in ("artist", "album", "playlist", "program", "folder"),
        playable=can_play or kind in ("track", "stream", "album", "playlist", "program"),
    )


#: Spotify playlist-id prefixes of playlists generated for one listener:
#: Discover Weekly and Release Radar (37i9dQZEVX), Daily Mixes and artist radio
#: mixes (37i9dQZF1E). Editorial playlists use 37i9dQZF1D. Undocumented and
#: observed rather than specified, so it only decides which section a playlist
#: is shown in; a miss still leaves it under "Your playlists".
PERSONAL_PREFIXES = ("spotify:playlist:37i9dQZEVX", "spotify:playlist:37i9dQZF1E")


def is_personal(entry: Entry) -> bool:
    return entry.kind == "playlist" and entry.id.startswith(PERSONAL_PREFIXES)


@dataclass
class ServiceInfo:
    name: str
    auth: str
    service_id: int
    service_type: int

    @property
    def needs_link(self) -> bool:
        return self.auth in ("AppLink", "DeviceLink", "UserId")


def _xml(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") \
        .replace('"', "&quot;")


class Services:
    """Access to every music service the household can use."""

    def __init__(self) -> None:
        self._store = None
        self._open: dict[tuple[str, str], object] = {}
        self._pending: dict[str, object] = {}

    @property
    def store(self):
        with _TOKEN_LOCK:  # cover lookups may be first to ask, several at once
            if self._store is None:
                path = token_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                # The file holds service authorisations. SoCo creates it with
                # the default mode, readable by every user on the machine;
                # create it private first, and tighten one that already exists.
                # Rewrites are private too (see `_token_store`).
                if not path.exists():
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as fh:
                        fh.write("{}")
                elif path.stat().st_mode & 0o077:
                    path.chmod(0o600)
                self._store = _token_store(path)
            return self._store

    # -- catalogue ---------------------------------------------------------

    def catalogue(self) -> list[ServiceInfo]:
        """Every service the household can use. Fetched once per session: the
        list comes from the speaker and does not change while the app runs."""
        if getattr(self, "_catalogue", None) is None:
            self._catalogue = self._fetch_catalogue()
        return self._catalogue

    @staticmethod
    def _fetch_catalogue() -> list[ServiceInfo]:
        from soco.music_services import MusicService

        out = []
        for name in MusicService.get_all_music_services_names():
            d = MusicService.get_data_for_name(name)
            try:
                out.append(ServiceInfo(name, d.get("Auth", "?"), int(d["ServiceID"]),
                                       int(d["ServiceType"])))
            except (KeyError, ValueError):
                continue
        return sorted(out, key=lambda s: s.name.lower())

    def info(self, name: str) -> ServiceInfo:
        for s in self.catalogue():
            if s.name == name:
                return s
        raise LookupError(f"no music service called {name!r}")

    def is_linked(self, name: str, speaker) -> bool:
        info = self.info(name)
        if not info.needs_link:
            return True
        try:
            return self.store.has_token(info.service_id, speaker.soco.household_id)
        except Exception:
            return False

    # -- a live service handle ----------------------------------------------

    def open(self, name: str, speaker):
        key = (name, speaker.uid)
        if key not in self._open:
            from soco.music_services import MusicService

            self._open[key] = MusicService(name, token_store=self.store, device=speaker.soco)
        return self._open[key]

    def cover_for(self, speaker, cover_url: str) -> str:
        """The service's own address for a queued song's cover, or "".

        The speaker makes covers one at a time, about a quarter of a second
        each, fetching every one from the service first. Asked directly, the
        service names its image server's address for many songs at once, and
        those load in parallel. Only services linked here are asked.
        """
        found = service_track(cover_url)
        if found is None:
            return ""
        sid, item_id = found
        info = next((s for s in self.catalogue() if s.service_id == sid), None)
        if info is None or not self.is_linked(info.name, speaker):
            return ""
        return art_address(self.open(info.name, speaker).get_media_metadata(item_id))

    def begin_link(self, name: str, speaker) -> str:
        """Start linking an AppLink/DeviceLink service. Returns the URL to open.

        The user logs in on the service's own site; this app never sees the
        password, only the token the service issues afterwards.
        """
        service = self.open(name, speaker)
        result = service.begin_authentication()
        self._pending[name] = service
        url = result if isinstance(result, str) else getattr(result, "reg_url", None) \
            or (result.get("regUrl") if isinstance(result, dict) else None)
        if not url:
            raise RuntimeError(f"{name} did not return a link address")
        return url

    def complete_link(self, name: str) -> None:
        service = self._pending.pop(name, None)
        if service is None:
            raise RuntimeError(f"no link in progress for {name}")
        service.complete_authentication()

    # -- browsing ----------------------------------------------------------

    def categories(self, name: str, speaker) -> list[str]:
        return list(self.open(name, speaker).available_search_categories)

    @staticmethod
    def _query(service, method: str, args: list, label: str) -> list:
        """Call SMAPI and parse the answer, surviving single results.

        SoCo only wraps a lone result in a list when the XML parser hands back
        an OrderedDict; current xmltodict returns a plain dict, so a search or
        folder with exactly one entry gets iterated as its own keys and raises.
        Normalising first avoids patching SoCo itself.
        """
        from soco.music_services.data_structures import parse_response

        response = service.soap_client.call(method, args)
        body = response.get("searchResult") or response.get("getMetadataResult") or {}
        for key in ("mediaCollection", "mediaMetadata"):
            if isinstance(body.get(key), dict):
                body[key] = [body[key]]
        return list(parse_response(service, response, label))

    def search(self, name: str, speaker, category: str, term: str, count: int = 50) -> list:
        service = self.open(name, speaker)
        prefix = service._get_search_prefix_map().get(category)
        if prefix is None:
            raise LookupError(f"{name} cannot search {category!r}")
        return self._query(service, "search", [
            ("id", prefix), ("term", term), ("index", 0), ("count", count),
        ], category)

    def browse(self, name: str, speaker, item=None, count: int = 100) -> list:
        """List a folder. `item` may be a result, a service id string, or None
        for the service's top level."""
        service = self.open(name, speaker)
        item_id = "root" if item is None else item if isinstance(item, str) else item.item_id
        if not isinstance(item, str) and item is not None:
            item_id = item.metadata.get("id", item_id)
        return self._query(service, "getMetadata", [
            ("id", item_id), ("index", 0), ("count", count), ("recursive", 0),
        ], "browse")

    @staticmethod
    def is_container(item) -> bool:
        meta = getattr(item, "metadata", {}) or {}
        kind = meta.get("item_type", "")
        return kind in ("container", "collection", "favorites", "search", "albumList",
                        "artist", "album", "playlist", "genre", "show", "program",
                        "artistTrackList", "trackList", "otherContainer") or bool(
            meta.get("can_enumerate") in (True, "true"))

    @staticmethod
    def is_stream(item) -> bool:
        return (getattr(item, "metadata", {}) or {}).get("item_type") == "stream"

    # -- playing -----------------------------------------------------------

    def stream_uri_and_meta(self, name: str, speaker, item) -> tuple[str, str]:
        """The URI and DIDL for a radio stream, as the official app sends them."""
        service = self.open(name, speaker)
        raw_id = item.metadata["id"]
        account = getattr(service, "account", None)
        serial = getattr(account, "serial_number", None) or 0
        uri = f"x-sonosapi-stream:{raw_id}?sid={service.service_id}&flags=8224&sn={serial}"
        desc = getattr(item, "desc", None) or f"SA_RINCON{service.service_type}_"
        meta = (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
            'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            f'<item id="{BROADCAST_PREFIX}{_xml(raw_id)}" parentID="L" restricted="true">'
            f"<dc:title>{_xml(item.title)}</dc:title>"
            "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
            f'<desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">'
            f"{_xml(desc)}</desc></item></DIDL-Lite>"
        )
        return uri, meta

    # -- the queue and Sonos playlists ---------------------------------------

    @staticmethod
    def enqueue(speaker, items: list, *, next_up: bool = False) -> int:
        """Add service items to the queue; returns the first position used.

        `next_up` inserts them straight after the current track instead of at
        the end. Radio cannot be queued and is skipped.
        """
        soco = speaker.soco
        position = 0
        if next_up:
            current = int(soco.get_current_track_info().get("playlist_position") or 0)
            position = current + 1 if current else 0
        first = 0
        for n, item in enumerate(i for i in items if describe(i).queueable):
            at = soco.add_to_queue(item, position=position + n if position else 0,
                                   as_next=next_up)
            first = first or at
        return first

    def play_context(self, name: str, speaker, items: list, start: int = 0) -> None:
        """Play `items[start]` and carry on down the rest of `items`.

        This is what double-clicking a song in an album or playlist means: the
        album continues afterwards. The queue is replaced by the list, as it is
        when playing an album in Spotify or the Sonos app. "Play next" and "Add
        to queue" (`enqueue`) are the ways to keep the existing queue.

        Radio has no queue position and is played directly instead.
        """
        target = items[start]
        if not describe(target).queueable:
            self.play(name, speaker, target)
            return
        queueable = [i for i in items if describe(i).queueable]
        soco = speaker.soco
        soco.clear_queue()
        if len(queueable) > 1 and all(describe(i).kind == "track" for i in queueable):
            # One request per sixteen songs rather than one per song.
            soco.add_multiple_to_queue(queueable)
            soco.play_from_queue(queueable.index(target))
        else:
            # A container (album, playlist, radio) expands to its songs on the
            # speaker; play it from the top.
            for item in queueable:
                soco.add_to_queue(item)
            soco.play_from_queue(0)

    def play_items(self, name: str, speaker, items: list) -> None:
        """Play a selection from its first item, continuing through the rest."""
        self.play_context(name, speaker, items, 0)

    @staticmethod
    def sonos_playlists(speaker) -> list:
        return list(speaker.soco.get_sonos_playlists(complete_result=True))

    @staticmethod
    def add_to_playlist(speaker, playlist, items: list) -> int:
        """Append items to a Sonos playlist. Albums and playlists are expanded
        by the speaker into their tracks. Returns how many items were sent."""
        sent = 0
        for item in items:
            if describe(item).queueable:
                speaker.soco.add_item_to_sonos_playlist(item, playlist)
                sent += 1
        return sent

    def new_playlist(self, speaker, title: str, items: list):
        playlist = speaker.soco.create_sonos_playlist(title)
        self.add_to_playlist(speaker, playlist, items)
        return playlist

    # -- pages -------------------------------------------------------------

    def lookup(self, name: str, speaker, item_id: str):
        """Turn a bare id, such as a track's artist id, into a full item.

        Needed for "Go to artist": a track names its artist only by id, and the
        page wants the artist's picture and name as well as their contents.
        """
        from soco.music_services.data_structures import parse_response

        service = self.open(name, speaker)
        found = service.get_extended_metadata(item_id)
        for key in ("mediaCollection", "mediaMetadata"):
            if isinstance(found.get(key), dict):
                wrapped = {"getMetadataResult": {"count": 1, key: [found[key]]}}
                items = list(parse_response(service, wrapped, "lookup"))
                if items:
                    return items[0]
        raise LookupError(f"{name} does not know {item_id}")

    #: What services call the folder holding the user's own music.
    LIBRARY_NAMES = ("your music", "my music", "your library", "my library", "library")

    def home_page(self, name: str, speaker) -> tuple[list, list[tuple[object, list]]]:
        """The service's top level, plus the user's own library opened up.

        Services file the user's playlists and albums one or two levels down
        (Spotify: Your Music › Playlists), which hides exactly what people look
        for first, such as Discover Weekly. Returns the top-level items and a
        list of ``(folder, items)`` for each folder inside the library.
        """
        root = self.browse(name, speaker)
        library = next((i for i in root
                        if (getattr(i, "title", "") or "").strip().lower() in self.LIBRARY_NAMES),
                       None)
        sections: list[tuple[object, list]] = []
        if library is not None:
            for folder in self.browse(name, speaker, library):
                if describe(folder).kind == "folder":
                    sections.append((folder, self.browse(name, speaker, folder, count=60)))
        return root, sections

    def artist_page(self, name: str, speaker, artist) -> tuple[list, list]:
        """An artist's contents plus their popular tracks, fetched together.

        Services list an artist as folders (a top-tracks list, radio, albums);
        opening the top-tracks folder as well lets the page show songs straight
        away, the way the official apps do.
        """
        children = self.browse(name, speaker, artist)
        top: list = []
        for child in children:
            md = getattr(child, "metadata", {}) or {}
            if md.get("item_type") in ("trackList", "artistTrackList"):
                top = self.browse(name, speaker, child, count=10)
                break
        return children, top

    def play(self, name: str, speaker, item, *, start: bool = True) -> None:
        """Play a single result by appending it to the queue.

        Radio replaces what is playing, as radio does in every Sonos app. The
        GUI plays through `play_context` instead, which continues down the list
        a song was picked from; this remains for the CLI's one-off plays.
        """
        if self.is_stream(item):
            uri, meta = self.stream_uri_and_meta(name, speaker, item)
            speaker.soco.play_uri(uri, meta=meta, title=item.title, start=start)
            return
        position = speaker.soco.add_to_queue(item)
        if start:
            speaker.soco.play_from_queue(position - 1)
