"""The application core: everything the UI needs, with no UI in it.

`Controller` owns the pieces that have to agree with each other — the discovered
speakers, the local library, the media server that publishes it, and the event
watchers that keep state fresh — and exposes the operations that need more than
one of them. Playing a local file, for instance, is only possible if the media
server is running, and the URL depends on which interface reaches the speaker.

Keeping this separate from the Qt front end means the same core drives the CLI,
and a different UI could be dropped on top without touching any of it.
"""

from __future__ import annotations

import io
import logging
import re
import threading
import wave
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs

from . import tts
from .config import Config
from .events import Watcher
from .library import Library
from .links import Link
from .mediaserver import MediaServer, list_monitors, token_for
from .speaker import _Loop, Speaker, discover
from .tags import Tags

log = logging.getLogger(__name__)

#: Sources a speaker names by the scheme of what it plays, not by a service.
_SCHEMES = {
    "x-rincon-stream": "Line-in", "x-sonos-htastream": "TV", "x-file-cifs": "Shared folder",
    "x-rincon-mp3radio": "Radio", "x-sonosapi-stream": "Radio", "x-sonosapi-radio": "Radio",
    "x-sonosapi-hls": "Radio", "aac": "Radio", "hls-radio": "Radio",
    "x-sonos-spotify": "Spotify",
}
#: A song served by this machine's media server: /music/<token>.<suffix>
_LOCAL = re.compile(r"/music/([0-9a-f]{20})\.\w+$")


def source_of(uri: str, service_name: Callable[[int], str] = lambda sid: "") -> str:
    """Where a song a speaker is playing comes from, in a word or two."""
    if not uri:
        return ""
    if "/stream/live." in uri:
        return "This computer"
    scheme = uri.split(":", 1)[0].lower()
    sid = parse_qs(uri.partition("?")[2]).get("sid", [""])[0]
    # A song from a service names it by number (Spotify is 9, TuneIn 303).
    if sid.isdigit() and (name := service_name(int(sid))):
        return name
    if scheme in _SCHEMES:
        return _SCHEMES[scheme]
    if scheme in ("http", "https"):
        return "Web link"
    return "A music service" if sid else ""


def wav_seconds(data: bytes) -> float:
    """How long a WAV recording plays, or 0 if it cannot be read."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            return w.getnframes() / w.getframerate()
    except (wave.Error, EOFError, ZeroDivisionError):
        return 0.0


def describe_format(track: Tags) -> str:
    """E.g. "FLAC · 44.1 kHz · stereo", leaving out what the file does not say."""
    parts = [track.path.suffix.lstrip(".").upper()]
    if track.sample_rate:
        parts.append(f"{track.sample_rate / 1000:g} kHz")
    if track.channels:
        parts.append({1: "mono", 2: "stereo"}.get(track.channels, f"{track.channels} channels"))
    return " · ".join(p for p in parts if p)


def _didl(title: str, creator: str, album: str, art: str, url: str, mime: str,
          duration: str = "") -> str:
    """DIDL-Lite metadata so the player shows title, artist and art.

    Without this a queued URL appears as the bare URL in every controller,
    including the official app; the speaker does not read tags itself.
    """
    def esc(text: str) -> str:
        return (
            (text or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    length = f' duration="{duration}"' if duration else ""
    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="-1" parentID="-1" restricted="true">'
        f"<dc:title>{esc(title)}</dc:title>"
        f"<dc:creator>{esc(creator)}</dc:creator>"
        f"<upnp:album>{esc(album)}</upnp:album>"
        f"<upnp:albumArtURI>{esc(art)}</upnp:albumArtURI>"
        # The res element tells the player what the resource is. Without it,
        # or a file extension on the URL, the player refuses to queue it
        # (UPnP 804).
        f'<res protocolInfo="http-get:*:{mime}:*"{length}>{esc(url)}</res>'
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        "</item></DIDL-Lite>"
    )


class Controller:
    """One per application run."""

    def __init__(self, music_dirs: list[Path] | None = None, config: Config | None = None) -> None:
        self.config = config if config is not None else Config.load()
        if music_dirs is None and self.config.music_dirs:
            music_dirs = [Path(d) for d in self.config.music_dirs if Path(d).is_dir()]
        self.library = Library(music_dirs)
        self.library.hidden = set(self.config.hidden_tracks)
        self.speakers: list[Speaker] = []
        self.server: MediaServer | None = None
        self._watchers: dict[str, Watcher] = {}
        self._lock = threading.RLock()

    # -- discovery ---------------------------------------------------------

    def refresh(self, timeout: int = 5) -> list[Speaker]:
        """Discover, then add back any remembered speaker that did not answer.

        A remembered speaker that turns out to be awake after all (SSDP is lossy)
        is picked up by the reachability check and marked awake; one that is
        genuinely asleep stays in the list, marked so, instead of vanishing.
        """
        found = discover(timeout)
        seen_uids = {sp.uid for sp in found}
        seen_ips = {sp.ip for sp in found}
        for sp in found:
            sp.awake = True
            self.config.remember(sp.ip, sp.name, sp.model, sp.uid)
        for known in self.config.speakers:
            if known.uid in seen_uids or known.ip in seen_ips:
                continue
            sp = Speaker(known.ip, name=known.name, model=known.model, uid=known.uid)
            sp.reachable()
            found.append(sp)
        found.sort(key=lambda s: (not s.awake, s.name.lower()))
        with self._lock:
            self.speakers = found
        self._save()
        return found

    def add_by_ip(self, ip: str) -> Speaker:
        """Add a speaker that discovery cannot see, e.g. across a VLAN.

        The address is checked before it is remembered, so a typo is refused
        rather than kept forever as a permanently asleep speaker.
        """
        sp = Speaker(ip.strip())
        if not sp.reachable(timeout=3):
            raise ConnectionError(f"nothing is answering on {ip}:1400")
        info = sp.info  # proves it is a Sonos player, not just an open port
        sp._model = info.get("model_name", "?")
        self.config.remember(sp.ip, sp.name, sp.model, sp.uid)
        with self._lock:
            self.speakers = [s for s in self.speakers if s.ip != sp.ip] + [sp]
        self._save()
        return sp

    def forget(self, speaker: Speaker) -> None:
        self.config.forget(speaker.ip)
        with self._lock:
            self.speakers = [s for s in self.speakers if s.ip != speaker.ip]
        self._save()

    def _save(self) -> None:
        self.config.music_dirs = [str(r) for r in self.library.roots]
        self.config.hidden_tracks = sorted(self.library.hidden)
        try:
            self.config.save()
        except OSError:
            log.warning("could not save settings", exc_info=True)

    def local_track(self, uri: str) -> Tags | None:
        """The Library song behind a speaker's URI, if this computer serves it.

        Matched by the token in the address rather than by the server's own
        address, so a song queued before the app last started is still found.
        """
        found = _LOCAL.search(uri.split("?", 1)[0])
        if not found:
            return None
        tracks = self.library.tracks
        if getattr(self, "_token_source", None) is not tracks:
            self._tokens = {token_for(t.path): t for t in tracks}
            self._token_source = tracks
        return self._tokens.get(found.group(1))

    def is_trusted(self, speaker: Speaker) -> bool:
        """Whether announcements may be sent to `speaker`: a fingerprint is saved."""
        from .ws import pin_for

        return bool(pin_for(self.config.websocket_fingerprints, speaker.uid, speaker.ip))

    def trust(self, speaker: Speaker, fingerprint: str) -> None:
        """Remember `speaker`'s certificate, by its own id so a new address keeps it.

        Only ever called after the user has seen the fingerprint and agreed.
        """
        self.config.websocket_fingerprints[speaker.uid or speaker.ip] = fingerprint
        self._save()
        speaker.close()  # the next announcement connects afresh with the pin

    def hide_tracks(self, tracks: list) -> None:
        self.library.hide(tracks)
        self._save()

    def get(self, uid_or_name: str) -> Speaker | None:
        needle = uid_or_name.lower()
        for sp in self.speakers:
            if sp.uid.lower() == needle or sp.name.lower() == needle:
                return sp
        return None

    # -- media server ------------------------------------------------------

    def start_server(self) -> str:
        """Start publishing the library. Idempotent.

        The server binds to whichever interface routes to a speaker, so it needs
        at least one to have been discovered; without that it would guess, and a
        wrong guess is silently unreachable from the player.
        """
        with self._lock:
            if self.server is not None and self.server.port is not None:
                return self.server.base_url
            peer = self.speakers[0].ip if self.speakers else "192.168.1.1"
            self.server = MediaServer(self.library, peer=peer, port=self.config.server_port)
        return _Loop.submit(self.server.start())

    def stop_server(self) -> None:
        with self._lock:
            server, self.server = self.server, None
        if server is not None:
            _Loop.submit(server.stop())

    @property
    def server_url(self) -> str | None:
        return self.server.base_url if self.server and self.server.port else None

    # -- local library playback -------------------------------------------

    def _require_server(self) -> MediaServer:
        self.start_server()
        assert self.server is not None
        return self.server

    def didl_for(self, track: Tags) -> str:
        """Metadata for a local track, served by the media server."""
        from .mediaserver import CONTENT_TYPES

        server = self._require_server()
        secs = int(track.duration)
        return _didl(
            track.display_title, track.artist or track.display_artist, track.album,
            server.art_url(track.path), server.music_url(track.path),
            CONTENT_TYPES.get(track.path.suffix.lower(), "audio/mpeg"),
            f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}",
        )

    @staticmethod
    def play_link(speaker: Speaker, link: Link) -> None:
        """Play a song from the web on its own; the speaker fetches it itself."""
        speaker.play_uri(link.url, meta=_didl(link.title, "", "", link.art, link.url, link.mime))

    @staticmethod
    def queue_link(speaker: Speaker, link: Link) -> int:
        """Add a song from the web to the end of the queue; returns its position."""
        reply = speaker.soco.avTransport.AddURIToQueue([
            ("InstanceID", 0),
            ("EnqueuedURI", link.url),
            ("EnqueuedURIMetaData", _didl(link.title, "", "", link.art, link.url, link.mime)),
            ("DesiredFirstTrackNumberEnqueued", 0),
            ("EnqueueAsNext", 0),
        ])
        return int(reply["FirstTrackNumberEnqueued"])

    def enqueue(self, speaker: Speaker, tracks: list[Tags], *, play_first: bool = False) -> int:
        """Add local tracks to a speaker's queue. Returns the first position used."""
        server = self._require_server()
        first = 0
        for track in tracks:
            # SoCo's add_uri_to_queue sends no metadata, and a queue entry
            # without DIDL shows as its bare URL in every controller, the
            # official app included. The raw action carries the metadata.
            reply = speaker.soco.avTransport.AddURIToQueue([
                ("InstanceID", 0),
                ("EnqueuedURI", server.music_url(track.path)),
                ("EnqueuedURIMetaData", self.didl_for(track)),
                ("DesiredFirstTrackNumberEnqueued", 0),
                ("EnqueueAsNext", 0),
            ])
            first = first or int(reply["FirstTrackNumberEnqueued"])
        if play_first and first:
            speaker.soco.play_from_queue(first - 1)
        return first

    @staticmethod
    def move_in_queue(speaker: Speaker, position: int, before: int) -> None:
        """Move the queue entry at `position` to just before `before` (1-based).

        This is the raw UPnP semantics: to move an entry down one place it must
        be inserted before the one *two* below it.
        """
        speaker.soco.avTransport.ReorderTracksInQueue([
            ("InstanceID", 0), ("StartingIndex", position), ("NumberOfTracks", 1),
            ("InsertBefore", before), ("UpdateID", 0),
        ])

    @staticmethod
    def save_queue(speaker: Speaker, title: str):
        """Save the current queue as a Sonos playlist every controller can see."""
        return speaker.soco.create_sonos_playlist_from_queue(title)

    def play_now(self, speaker: Speaker, track: Tags) -> None:
        """Replace what is playing with one local track."""
        server = self._require_server()
        speaker.soco.play_uri(
            server.music_url(track.path), meta=self.didl_for(track), title=track.display_title
        )

    # -- favourites, playlists, alarms --------------------------------------

    #: URI schemes that are live streams. These are played directly; anything
    #: else is a container or track and goes through the queue.
    STREAM_SCHEMES = (
        "x-sonosapi-stream:", "x-sonosapi-radio:", "x-rincon-mp3radio:",
        "x-sonosapi-hls:", "x-sonosapi-hls-static:", "aac:", "hls-radio:",
    )

    def favourites(self, speaker: Speaker) -> list:
        return list(speaker.soco.music_library.get_sonos_favorites(complete_result=True))

    def playlists(self, speaker: Speaker) -> list:
        return list(speaker.soco.get_sonos_playlists(complete_result=True))

    def play_favourite(self, speaker: Speaker, fav) -> None:
        """Play a Sonos favourite the way the official app does.

        A favourite is a pointer to something else — a radio station, a service
        album, a playlist. Streams have no queue and are played directly;
        everything else replaces the queue and plays from the top, like playing
        an album anywhere else in the app.
        """
        uri = fav.get_uri() if getattr(fav, "resources", None) else ""
        meta = getattr(fav, "resource_meta_data", "") or ""
        if uri.startswith(self.STREAM_SCHEMES):
            speaker.soco.play_uri(uri, meta=meta, title=fav.title)
            return
        speaker.soco.clear_queue()
        try:
            speaker.soco.add_to_queue(fav.reference)
        except Exception:
            # Some service items cannot be enqueued from a favourite reference;
            # handing the URI straight to the player still works for those.
            if not uri:
                raise
            speaker.soco.play_uri(uri, meta=meta, title=fav.title)
            return
        speaker.soco.play_from_queue(0)

    def play_playlist(self, speaker: Speaker, playlist) -> None:
        speaker.soco.clear_queue()
        speaker.soco.add_to_queue(playlist)
        speaker.soco.play_from_queue(0)

    def play_local_list(self, speaker: Speaker, tracks: list[Tags], start: int = 0) -> None:
        """Play local files from `tracks[start]` on, continuing down the list.

        Double-clicking a song in the Library means "this album from here", not
        "this one file"; playing the bare file would stop when it ends.
        """
        speaker.soco.clear_queue()
        self.enqueue(speaker, tracks)
        speaker.soco.play_from_queue(start)

    def alarms(self, speaker: Speaker) -> list:
        """Every alarm in the household, soonest first.

        Alarms belong to the household rather than to one speaker, so this lists
        alarms for every room; the UI shows which room each one wakes.
        """
        from soco.alarms import get_alarms

        found = list(get_alarms(speaker.soco))
        return sorted(found, key=lambda a: (a.start_time, a.zone.player_name))

    def create_alarm(
        self, speaker: Speaker, start, *, recurrence: str = "DAILY", volume: int = 20,
        duration=None, enabled: bool = True,
    ):
        from soco.alarms import Alarm, is_valid_recurrence

        if not is_valid_recurrence(recurrence):
            raise ValueError(f"not a Sonos recurrence: {recurrence!r}")
        alarm = Alarm(
            speaker.soco, start_time=start, duration=duration, recurrence=recurrence,
            enabled=enabled, volume=max(0, min(100, int(volume))),
        )
        alarm.save()
        return alarm

    @staticmethod
    def set_alarm_enabled(alarm, enabled: bool) -> None:
        alarm.enabled = bool(enabled)
        alarm.save()

    @staticmethod
    def delete_alarm(alarm) -> None:
        alarm.remove()

    # -- live capture ------------------------------------------------------

    def stream_desktop(self, speaker: Speaker, fmt: str = "flac") -> str:
        """Send this machine's audio output to a speaker.

        The speaker pulls the stream, so it keeps playing until something else
        takes the player over. This is noson-app's headline feature and has no
        equivalent in any other Python Sonos library.
        """
        server = self._require_server()
        url = _Loop.submit(server.start_stream(fmt))
        try:
            speaker.soco.play_uri(url, title="Desktop audio")
        except Exception:
            _Loop.submit(server.stop_stream())
            raise
        return url

    def stop_desktop(self) -> None:
        """Stop publishing desktop audio, even if a speaker is unreachable."""
        if self.server is not None:
            _Loop.submit(self.server.stop_stream())

    def capture_sources(self) -> list[str]:
        return _Loop.submit(list_monitors())

    def set_capture_source(self, source: str | None) -> None:
        server = self._require_server()
        server.capture_source = source

    # -- notifications -----------------------------------------------------

    def say(
        self,
        speaker: Speaker,
        text: str,
        *,
        voice: str = "en",
        wpm: int = 165,
        volume: int | None = None,
    ) -> dict:
        """Speak `text` on a speaker, over the music rather than instead of it."""
        server = self._require_server()
        wav = _Loop.submit(tts.synthesise(text, voice=voice, wpm=wpm))
        url = server.add_clip(wav, "audio/wav", "wav")
        self.last_clip_url = url
        reply = speaker.announce(url, volume=volume, name="Sonolin speech")
        # How long it speaks, so the app knows how long it can be stopped for.
        return {**reply, "seconds": wav_seconds(wav)}

    def wait_clip_fetched(self, timeout: float = 20.0) -> bool:
        """Block until the speaker has fetched the most recent clip."""
        url = getattr(self, "last_clip_url", None)
        if not url or self.server is None:
            return False
        return _Loop.submit(self.server.wait_fetched(url, timeout), timeout=timeout + 5)

    def notify(self, speaker: Speaker, url: str, *, volume: int | None = None) -> dict:
        """Play an arbitrary sound over the music."""
        return speaker.announce(url, volume=volume, name="Sonolin notification")

    # -- live state --------------------------------------------------------

    def watch(self, speaker: Speaker, on_change: Callable[[str, dict], None]) -> None:
        """Subscribe to a speaker's state changes.

        The callback runs on SoCo's listener thread, so a UI must marshal it.
        """
        with self._lock:
            if speaker.uid in self._watchers:
                return
            watcher = Watcher(speaker, on_change)
            self._watchers[speaker.uid] = watcher
        watcher.start()

    def unwatch(self, speaker: Speaker) -> None:
        with self._lock:
            watcher = self._watchers.pop(speaker.uid, None)
        if watcher:
            watcher.stop()

    def close(self) -> None:
        for uid in list(self._watchers):
            watcher = self._watchers.pop(uid, None)
            if watcher:
                watcher.stop()
        self.stop_server()
        for sp in self.speakers:
            sp.close()
