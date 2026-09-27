"""One object per Sonos player, covering both control protocols.

Sonos exposes two independent control surfaces and they do not overlap:

* **UPnP/SOAP on port 1400** — the original protocol. Everything to do with the
  queue, the music library, saved playlists, alarms, EQ, grouping and device
  settings lives here. Reached through SoCo.
* **WebSocket on port 1443** — the modern Sonos Control API spoken locally.
  Audio clips (announcements layered over the music), volume ducking and
  home-theater options exist only here.

`Speaker` presents both as one object and starts the websocket lazily, so a
program that never announces anything never opens the second connection.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import socket

import soco
import soco.config
from soco.snapshot import Snapshot

from .ws import SonosWebSocket

log = logging.getLogger(__name__)

#: Seconds before any single request to a speaker gives up. SoCo's own default
#: is long enough to freeze a UI; a speaker that has not answered in this long
#: is asleep or gone, and waiting longer will not change that.
soco.config.REQUEST_TIMEOUT = 5

PLAY_MODES = ("NORMAL", "REPEAT_ALL", "REPEAT_ONE", "SHUFFLE_NOREPEAT", "SHUFFLE", "SHUFFLE_REPEAT_ONE")


class _Loop:
    """A background asyncio loop shared by every Speaker in the process.

    The websocket half of the API is asyncio-only while SoCo is blocking, and a
    desktop UI is neither. Rather than force one model on callers, the async work
    runs on one daemon thread and `submit` bridges into it from anywhere.
    """

    _lock = threading.Lock()
    _loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    def get(cls) -> asyncio.AbstractEventLoop:
        with cls._lock:
            if cls._loop is None or cls._loop.is_closed():
                cls._loop = asyncio.new_event_loop()
                threading.Thread(
                    target=cls._loop.run_forever, name="sonolin-aio", daemon=True
                ).start()
            return cls._loop

    @classmethod
    def submit(cls, coro, timeout: float = 15.0):
        return asyncio.run_coroutine_threadsafe(coro, cls.get()).result(timeout)


class Speaker:
    """A single Sonos player."""

    def __init__(self, ip: str, *, name: str = "", model: str = "", uid: str = "") -> None:
        self.ip = ip
        self.soco = soco.SoCo(ip)
        self._ws: SonosWebSocket | None = None
        self._info: dict[str, Any] | None = None
        # Remembered identity, so a speaker that is asleep can still be shown
        # by name. Replaced by the live value the first time it answers.
        self._name = name
        self._model = model
        self._uid = uid
        self.awake = True

    # -- identity ----------------------------------------------------------

    def __repr__(self) -> str:
        return f"<Speaker {self.name!r} {self.ip}>"

    @property
    def name(self) -> str:
        if not self._name:
            self._name = self.soco.player_name
        return self._name

    @property
    def uid(self) -> str:
        """The player id, e.g. ``RINCON_48A6B8EEA8C001400``."""
        if not self._uid:
            self._uid = self.soco.uid
        return self._uid

    def reachable(self, timeout: float = 1.5) -> bool:
        """Whether the player is answering at all, without a SOAP round trip.

        A sleeping Move or Roam still answers ARP from its wifi chip, so ping and
        the neighbour table both claim it is there; only a TCP connect to the
        control port tells the truth.
        """
        try:
            with socket.create_connection((self.ip, 1400), timeout=timeout):
                self.awake = True
        except OSError:
            self.awake = False
        return self.awake

    @property
    def info(self) -> dict[str, Any]:
        if self._info is None:
            self._info = self.soco.get_speaker_info()
        return self._info

    @property
    def model(self) -> str:
        if not self._model:
            self._model = self.info.get("model_name", "?")
        return self._model

    @property
    def firmware(self) -> str:
        return self.info.get("software_version", "?")

    # -- websocket tier ----------------------------------------------------

    @property
    def ws(self) -> SonosWebSocket:
        if self._ws is None:
            self._ws = SonosWebSocket(self.ip)
            _Loop.submit(self._ws.learn_household())
        return self._ws

    def ws_command(self, namespace: str, command: str, *, target: str | None = None, **body):
        """Send one websocket command, blocking until the reply arrives."""
        if target is None:
            from .ws import TARGET

            key = TARGET.get(namespace)
            if key == "playerId":
                target = self.uid
            elif key == "groupId":
                target = self.group_id
            elif key == "householdId":
                target = self.ws.household_id
        return _Loop.submit(self.ws.command(namespace, command, target=target, **body))

    @property
    def group_id(self) -> str:
        """The websocket group id, which is not the UPnP group id.

        UPnP identifies a group as ``<coordinator uid>:<token>``; the websocket
        uses a different token for the same group, so it has to be read from the
        groups namespace rather than constructed.
        """
        hh = self.ws.household_id
        groups = _Loop.submit(self.ws.command("groups", "getGroups", target=hh))
        for g in groups["groups"]:
            if self.uid in g["playerIds"]:
                return g["id"]
        raise LookupError(f"{self.uid} is in no group")

    @property
    def capabilities(self) -> list[str]:
        """What the player itself says it can do, e.g. ``AUDIO_CLIP``, ``LINE_IN``."""
        hh = self.ws.household_id
        groups = _Loop.submit(self.ws.command("groups", "getGroups", target=hh))
        for p in groups["players"]:
            if p["id"] == self.uid:
                return p.get("capabilities", [])
        return []

    # -- announcements -----------------------------------------------------

    def announce(
        self,
        url: str | None = None,
        *,
        volume: int | None = None,
        name: str = "Sonolin",
        clip_type: str | None = None,
    ) -> dict:
        """Play a short clip over whatever is already playing.

        The music ducks, the clip plays, the music comes back up: no queue
        change, no snapshot, no restore. `clip_type` plays a sound built into the
        player (``CHIME``) and needs no URL at all; otherwise `url` must be
        reachable by the speaker, not just by this machine.

        This is only available over the websocket tier. Players report
        ``AUDIO_CLIP`` in `capabilities` when they support it.
        """
        body: dict[str, Any] = {"name": name, "appId": "com.sonolin.control"}
        if clip_type:
            body["clipType"] = clip_type
        if url:
            body["streamUrl"] = url
        if volume is not None:
            body["volume"] = int(volume)
        return self.ws_command("audioClip", "loadAudioClip", **body)

    def duck(self) -> dict:
        """Drop the group to a low volume without touching the volume setting."""
        return self.ws_command("playerVolume", "duck")

    def unduck(self) -> dict:
        return self.ws_command("playerVolume", "unduck")

    # -- transport ---------------------------------------------------------

    def play(self) -> None:
        self.soco.play()

    def pause(self) -> None:
        self.soco.pause()

    def stop(self) -> None:
        self.soco.stop()

    def next(self) -> None:
        self.soco.next()

    def previous(self) -> None:
        self.soco.previous()

    def seek(self, timestamp: str) -> None:
        """Seek within the track; `timestamp` is ``HH:MM:SS``."""
        self.soco.seek(timestamp)

    @property
    def state(self) -> str:
        return self.soco.get_current_transport_info()["current_transport_state"]

    @property
    def track(self) -> dict[str, Any]:
        return self.soco.get_current_track_info()

    # -- volume and sound --------------------------------------------------

    def _get_volume(self) -> int:
        return self.soco.volume

    def _set_volume(self, value: int) -> None:
        self.soco.volume = max(0, min(100, int(value)))

    volume = property(_get_volume, _set_volume)

    def relative_volume(self, delta: int) -> None:
        self.soco.set_relative_volume(int(delta))

    def ramp_to_volume(self, target: int) -> int:
        """Slide to `target` instead of jumping, the way an alarm does."""
        return self.soco.ramp_to_volume(max(0, min(100, int(target))))

    @property
    def group_volume(self) -> int:
        return int(self.soco.groupRenderingControl.GetGroupVolume([("InstanceID", 0)])["CurrentVolume"])

    @group_volume.setter
    def group_volume(self, value: int) -> None:
        self.soco.groupRenderingControl.SetGroupVolume(
            [("InstanceID", 0), ("DesiredVolume", max(0, min(100, int(value))))]
        )

    def relative_group_volume(self, delta: int) -> int:
        return int(
            self.soco.groupRenderingControl.SetRelativeGroupVolume(
                [("InstanceID", 0), ("Adjustment", int(delta))]
            )["NewVolume"]
        )

    def sound(self) -> dict[str, Any]:
        """Every tone and spatial setting the player admits to having.

        `None` means the model has no such control, which is how the soundbar
        settings are told apart from the portable ones.
        """
        keys = (
            "bass", "treble", "loudness", "balance", "night_mode", "dialog_mode",
            "dialog_level", "cross_fade", "sub_enabled", "sub_gain", "sub_crossover",
            "surround_enabled", "surround_level", "surround_mode",
            "music_surround_level", "surround_volume_tv", "surround_volume_music",
            "surround_full_volume_enabled", "audio_delay", "trueplay", "fixed_volume",
        )
        out = {}
        for k in keys:
            try:
                out[k] = getattr(self.soco, k)
            except Exception as exc:  # model does not implement this EQ type
                log.debug("%s: %s unavailable: %s", self.name, k, exc)
                out[k] = None
        return out

    # -- grouping ----------------------------------------------------------

    @property
    def is_coordinator(self) -> bool:
        return self.soco.is_coordinator

    def join(self, other: "Speaker") -> None:
        """Add this player to `other`'s group."""
        self.soco.join(other.soco)

    def unjoin(self) -> None:
        self.soco.unjoin()

    def party_mode(self) -> None:
        """Pull every visible player into one group behind this one."""
        self.soco.partymode()

    def create_stereo_pair(self, right: "Speaker") -> None:
        """Bond two identical players as a stereo pair, this one on the left."""
        self.soco.create_stereo_pair(right.soco)

    def separate_stereo_pair(self) -> None:
        self.soco.separate_stereo_pair()

    # -- state capture -----------------------------------------------------

    def snapshot(self, *, include_queue: bool = False) -> Snapshot:
        """Capture what is playing so it can be put back afterwards.

        Needed when doing something intrusive, such as taking over the queue for
        a notification. For a plain announcement prefer `announce`, which does
        not disturb playback at all.
        """
        snap = Snapshot(self.soco, snapshot_queue=include_queue)
        snap.snapshot()
        return snap

    @staticmethod
    def restore(snap: Snapshot, *, fade: bool = False) -> None:
        snap.restore(fade=fade)

    # -- device ------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Everything about the unit itself rather than what it is playing."""
        out: dict[str, Any] = {
            "name": self.name,
            "uid": self.uid,
            "ip": self.ip,
            "model": self.model,
            "firmware": self.firmware,
            "coordinator": self.is_coordinator,
        }
        for k in ("status_light", "buttons_enabled", "mic_enabled",
                  "voice_service_configured", "is_soundbar", "has_subwoofer",
                  "has_satellites", "is_satellite", "is_visible"):
            try:
                out[k] = getattr(self.soco, k)
            except Exception:
                out[k] = None
        try:
            out["battery"] = self.soco.get_battery_info()
        except Exception:
            out["battery"] = None  # mains-powered model
        return out

    def _get_led(self) -> bool:
        return self.soco.status_light

    def _set_led(self, on: bool) -> None:
        self.soco.status_light = bool(on)

    led = property(_get_led, _set_led)

    def _get_buttons(self) -> bool:
        return self.soco.buttons_enabled

    def _set_buttons(self, on: bool) -> None:
        self.soco.buttons_enabled = bool(on)

    buttons = property(_get_buttons, _set_buttons)

    # -- sources -----------------------------------------------------------

    def play_uri(self, uri: str, *, meta: str = "", title: str = "", start: bool = True) -> None:
        """Play any URI the speaker can fetch: a stream, an HTTP file, a radio."""
        self.soco.play_uri(uri, meta=meta, title=title, start=start)

    def switch_to_line_in(self, source: "Speaker | None" = None) -> None:
        self.soco.switch_to_line_in(source.soco if source else None)

    def switch_to_tv(self) -> None:
        self.soco.switch_to_tv()

    def sleep_timer(self, seconds: int | None) -> None:
        """Set the sleep timer, or pass `None` to cancel it."""
        self.soco.set_sleep_timer(seconds)

    def close(self) -> None:
        if self._ws is not None:
            _Loop.submit(self._ws.close())
            self._ws = None


def discover(timeout: int = 5, *, household: str | None = None) -> list[Speaker]:
    """Find every player on the LAN by SSDP multicast.

    `household` restricts the search to one system, which matters only where two
    Sonos systems share a network. SoCo's default already matches any household,
    so it is left alone unless asked for.
    """
    kwargs = {"household_id": household} if household else {}
    # SSDP is UDP multicast: a single query is genuinely lost often enough that
    # one empty result means nothing. Retry before falling back to the much
    # slower unicast sweep of the subnet.
    for attempt in range(2):
        zones = soco.discover(timeout=timeout, **kwargs) or set()
        if zones:
            break
        log.debug("discovery attempt %d found nothing", attempt + 1)
    else:
        log.info("SSDP found nothing; scanning the subnet instead")
        zones = soco.discover(timeout=timeout, allow_network_scan=True, **kwargs) or set()
    found = []
    for z in zones:
        sp = Speaker(z.ip_address)
        try:
            sp._name, sp._uid = z.player_name, z.uid
        except Exception:
            log.debug("%s answered SSDP but not SOAP", z.ip_address)
            continue
        found.append(sp)
    return sorted(found, key=lambda s: s.name)


def by_name(name: str, timeout: int = 5) -> Speaker:
    """Find one player by room name, case-insensitively."""
    wanted = name.strip().lower()
    for sp in discover(timeout):
        if sp.name.lower() == wanted:
            return sp
    raise LookupError(f"no Sonos player named {name!r}")
