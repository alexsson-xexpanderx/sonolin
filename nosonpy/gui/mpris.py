"""MPRIS 2 on the session bus: media keys, the Plasma/GNOME media widgets and
the lock screen can drive whichever speaker the window has selected.

noson-app does the same in C++ (`backend/NosonApp/dbus/mpris2.cpp`).

The spec is strict about wire types and desktop shells are strict about the
spec: `mpris:length` and `Position` must be int64 (``x``), `mpris:trackid` an
object path (``o``), and `PropertiesChanged` must carry an ``as``. PyQt picks
D-Bus types from Python values — an int becomes ``i`` — so every value whose
type matters is built as an explicitly typed `QVariant` or `QDBusArgument`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PyQt6.QtCore import (
    QMetaType, QObject, QVariant, pyqtClassInfo, pyqtProperty, pyqtSignal, pyqtSlot,
)
from PyQt6.QtDBus import (
    QDBusAbstractAdaptor, QDBusArgument, QDBusConnection, QDBusMessage, QDBusObjectPath,
)

log = logging.getLogger(__name__)

SERVICE = "org.mpris.MediaPlayer2.nosonpy"
PATH = "/org/mpris/MediaPlayer2"
ROOT_IFACE = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"


def _int64(value: int) -> QVariant:
    v = QVariant(int(value))
    v.convert(QMetaType(QMetaType.Type.LongLong.value))
    return v


def _string_list(items: list[str]) -> QDBusArgument:
    """An ``as``, including when empty — a bare [] would marshal as ``av``."""
    arg = QDBusArgument()
    arg.beginArray(QMetaType.Type.QString.value)
    for item in items:
        arg.add(item)
    arg.endArray()
    return arg


ROOT_XML = """
<interface name="org.mpris.MediaPlayer2">
  <method name="Raise"/>
  <method name="Quit"/>
  <property name="CanQuit" type="b" access="read"/>
  <property name="CanRaise" type="b" access="read"/>
  <property name="HasTrackList" type="b" access="read"/>
  <property name="Identity" type="s" access="read"/>
  <property name="DesktopEntry" type="s" access="read"/>
  <property name="SupportedUriSchemes" type="as" access="read"/>
  <property name="SupportedMimeTypes" type="as" access="read"/>
</interface>
"""

PLAYER_XML = """
<interface name="org.mpris.MediaPlayer2.Player">
  <method name="Next"/>
  <method name="Previous"/>
  <method name="Pause"/>
  <method name="PlayPause"/>
  <method name="Stop"/>
  <method name="Play"/>
  <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
  <method name="SetPosition">
    <arg direction="in" name="TrackId" type="o"/>
    <arg direction="in" name="Position" type="x"/>
  </method>
  <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
  <signal name="Seeked"><arg name="Position" type="x"/></signal>
  <property name="PlaybackStatus" type="s" access="read"/>
  <property name="LoopStatus" type="s" access="readwrite"/>
  <property name="Rate" type="d" access="readwrite"/>
  <property name="Shuffle" type="b" access="readwrite"/>
  <property name="Metadata" type="a{sv}" access="read"/>
  <property name="Volume" type="d" access="readwrite"/>
  <property name="Position" type="x" access="read"/>
  <property name="MinimumRate" type="d" access="read"/>
  <property name="MaximumRate" type="d" access="read"/>
  <property name="CanGoNext" type="b" access="read"/>
  <property name="CanGoPrevious" type="b" access="read"/>
  <property name="CanPlay" type="b" access="read"/>
  <property name="CanPause" type="b" access="read"/>
  <property name="CanSeek" type="b" access="read"/>
  <property name="CanControl" type="b" access="read"/>
</interface>
"""


@pyqtClassInfo("D-Bus Interface", ROOT_IFACE)
@pyqtClassInfo("D-Bus Introspection", ROOT_XML)
class RootAdaptor(QDBusAbstractAdaptor):
    def __init__(self, service: "Mpris") -> None:
        super().__init__(service)
        self.service = service

    @pyqtSlot()
    def Raise(self) -> None:
        self.service.raise_requested.emit()

    @pyqtSlot()
    def Quit(self) -> None:
        self.service.quit_requested.emit()

    @pyqtProperty(bool)
    def CanQuit(self) -> bool:
        return True

    @pyqtProperty(bool)
    def CanRaise(self) -> bool:
        return True

    @pyqtProperty(bool)
    def HasTrackList(self) -> bool:
        return False

    @pyqtProperty(str)
    def Identity(self) -> str:
        return "nosonpy"

    @pyqtProperty(str)
    def DesktopEntry(self) -> str:
        return "nosonpy"

    @pyqtProperty("QStringList")
    def SupportedUriSchemes(self) -> list[str]:
        return []

    @pyqtProperty("QStringList")
    def SupportedMimeTypes(self) -> list[str]:
        return []


@pyqtClassInfo("D-Bus Interface", PLAYER_IFACE)
@pyqtClassInfo("D-Bus Introspection", PLAYER_XML)
class PlayerAdaptor(QDBusAbstractAdaptor):
    Seeked = pyqtSignal("qlonglong")

    def __init__(self, service: "Mpris") -> None:
        super().__init__(service)
        self.setAutoRelaySignals(False)
        self.service = service

    # -- methods -----------------------------------------------------------

    @pyqtSlot()
    def Next(self) -> None:
        self.service.command.emit("next", None)

    @pyqtSlot()
    def Previous(self) -> None:
        self.service.command.emit("previous", None)

    @pyqtSlot()
    def Pause(self) -> None:
        self.service.command.emit("pause", None)

    @pyqtSlot()
    def PlayPause(self) -> None:
        self.service.command.emit("playpause", None)

    @pyqtSlot()
    def Stop(self) -> None:
        self.service.command.emit("stop", None)

    @pyqtSlot()
    def Play(self) -> None:
        self.service.command.emit("play", None)

    @pyqtSlot("qlonglong")
    def Seek(self, offset_us: int) -> None:
        target = max(0.0, self.service.position + offset_us / 1e6)
        if self.service.length and target > self.service.length:
            self.service.command.emit("next", None)  # the spec says to skip
        else:
            self.service.command.emit("seek", target)

    @pyqtSlot(QDBusObjectPath, "qlonglong")
    def SetPosition(self, track_id: QDBusObjectPath, position_us: int) -> None:
        # Ignored if it names a track other than the current one, per the spec.
        if track_id.path() == self.service.track_id and 0 <= position_us:
            self.service.command.emit("seek", position_us / 1e6)

    @pyqtSlot(str)
    def OpenUri(self, uri: str) -> None:
        self.service.command.emit("open", uri)

    # -- properties ----------------------------------------------------------

    @pyqtProperty(str)
    def PlaybackStatus(self) -> str:
        return self.service.status

    def _get_loop(self) -> str:
        return self.service.loop

    def _set_loop(self, value: str) -> None:
        self.service.command.emit("loop", value)

    LoopStatus = pyqtProperty(str, _get_loop, _set_loop)

    def _get_rate(self) -> float:
        return 1.0

    def _set_rate(self, value: float) -> None:
        pass  # Sonos plays at one speed only

    Rate = pyqtProperty(float, _get_rate, _set_rate)

    def _get_shuffle(self) -> bool:
        return self.service.shuffle

    def _set_shuffle(self, value: bool) -> None:
        self.service.command.emit("shuffle", bool(value))

    Shuffle = pyqtProperty(bool, _get_shuffle, _set_shuffle)

    @pyqtProperty("QVariantMap")
    def Metadata(self) -> dict:
        return self.service.metadata()

    def _get_volume(self) -> float:
        return self.service.volume

    def _set_volume(self, value: float) -> None:
        self.service.command.emit("volume", max(0.0, min(1.0, float(value))))

    Volume = pyqtProperty(float, _get_volume, _set_volume)

    @pyqtProperty("qlonglong")
    def Position(self) -> int:
        return int(self.service.position * 1e6)

    @pyqtProperty(float)
    def MinimumRate(self) -> float:
        return 1.0

    @pyqtProperty(float)
    def MaximumRate(self) -> float:
        return 1.0

    @pyqtProperty(bool)
    def CanGoNext(self) -> bool:
        return self.service.has_speaker

    @pyqtProperty(bool)
    def CanGoPrevious(self) -> bool:
        return self.service.has_speaker

    @pyqtProperty(bool)
    def CanPlay(self) -> bool:
        return self.service.has_speaker

    @pyqtProperty(bool)
    def CanPause(self) -> bool:
        return self.service.has_speaker

    @pyqtProperty(bool)
    def CanSeek(self) -> bool:
        return self.service.has_speaker and self.service.length > 0

    @pyqtProperty(bool)
    def CanControl(self) -> bool:
        return True


class Mpris(QObject):
    """The object registered on the bus, and the window's handle on it.

    The window pushes state in with `update`; commands from the desktop come
    out of the `command` signal as ``(name, argument)``, on the GUI thread.
    """

    command = pyqtSignal(str, object)
    raise_requested = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.status = "Stopped"
        self.loop = "None"
        self.shuffle = False
        self.volume = 0.0
        self.position = 0.0
        self.length = 0.0
        self.title = self.artist = self.album = self.art = self.uri = ""
        self.track_id = NO_TRACK
        self.has_speaker = False
        self._root = RootAdaptor(self)
        self._player = PlayerAdaptor(self)
        self.bus = QDBusConnection.sessionBus()
        self.registered = False

    def register(self) -> bool:
        if not self.bus.isConnected():
            log.info("no session bus; media keys will not work")
            return False
        if not self.bus.registerObject(PATH, self):
            log.warning("could not register %s", PATH)
            return False
        if not self.bus.registerService(SERVICE):
            # Another copy is running; use a unique suffix, as the spec allows.
            import os

            alt = f"{SERVICE}.instance{os.getpid()}"
            if not self.bus.registerService(alt):
                log.warning("could not claim an MPRIS bus name")
                return False
        self.registered = True
        return True

    def unregister(self) -> None:
        if self.registered:
            self.bus.unregisterObject(PATH)
            self.bus.unregisterService(SERVICE)
            self.registered = False

    # -- state in ------------------------------------------------------------

    def metadata(self) -> dict:
        meta: dict[str, object] = {"mpris:trackid": QDBusObjectPath(self.track_id)}
        if self.length:
            meta["mpris:length"] = _int64(self.length * 1e6)
        if self.title:
            meta["xesam:title"] = self.title
        if self.artist:
            meta["xesam:artist"] = _string_list([self.artist])
        if self.album:
            meta["xesam:album"] = self.album
        if self.art:
            meta["mpris:artUrl"] = self.art
        if self.uri:
            meta["xesam:url"] = self.uri
        return meta

    def update(
        self, *, state: str | None = None, title: str = "", artist: str = "",
        album: str = "", art: str = "", uri: str = "", length: float = 0.0,
        position: float | None = None, volume: int | None = None,
        shuffle: bool | None = None, repeat: object = None, has_speaker: bool = True,
    ) -> None:
        """Take the latest state and announce whatever actually changed."""
        changed: dict[str, object] = {}
        status = {"PLAYING": "Playing", "TRANSITIONING": "Playing",
                  "PAUSED_PLAYBACK": "Paused"}.get(state or "", "Stopped")
        if status != self.status:
            self.status = status
            changed["PlaybackStatus"] = status

        track_changed = (title, artist, album, art, uri, round(length)) != (
            self.title, self.artist, self.album, self.art, self.uri, round(self.length))
        if track_changed:
            self.title, self.artist, self.album, self.art, self.uri = (
                title, artist, album, art, uri)
            self.length = length
            # A fresh id per track is how a client knows the track changed.
            self.track_id = f"/org/nosonpy/track/{abs(hash((title, artist, album, uri)))}"
            changed["Metadata"] = self.metadata()
            changed["CanSeek"] = has_speaker and length > 0

        if has_speaker != self.has_speaker:
            self.has_speaker = has_speaker
            for key in ("CanGoNext", "CanGoPrevious", "CanPlay", "CanPause"):
                changed[key] = has_speaker

        if volume is not None and abs(volume / 100 - self.volume) > 0.001:
            self.volume = volume / 100
            changed["Volume"] = self.volume

        if shuffle is not None and shuffle != self.shuffle:
            self.shuffle = shuffle
            changed["Shuffle"] = shuffle

        if repeat is not None:
            loop = {True: "Playlist", "ONE": "Track"}.get(repeat, "None")
            if loop != self.loop:
                self.loop = loop
                changed["LoopStatus"] = loop

        if position is not None:
            # Position is not announced through PropertiesChanged (the spec
            # forbids it); only jumps are, through Seeked.
            expected = self.position
            self.position = position
            if abs(position - expected) > 3 and not track_changed:
                self._emit_seeked(position)

        if changed and self.registered:
            self._properties_changed(changed)

    def advance(self, seconds: float) -> None:
        """Keep the local position estimate moving between real polls."""
        if self.status == "Playing":
            self.position = min(self.position + seconds, self.length or self.position + seconds)

    # -- signals out ---------------------------------------------------------

    def _properties_changed(self, changed: dict) -> None:
        msg = QDBusMessage.createSignal(PATH, "org.freedesktop.DBus.Properties",
                                        "PropertiesChanged")
        msg.setArguments([PLAYER_IFACE, changed, _string_list([])])
        self.bus.send(msg)

    def _emit_seeked(self, position: float) -> None:
        if not self.registered:
            return
        msg = QDBusMessage.createSignal(PATH, PLAYER_IFACE, "Seeked")
        msg.setArguments([_int64(position * 1e6)])
        self.bus.send(msg)


def connect(window, mpris: Mpris, handlers: dict[str, Callable]) -> None:
    """Wire desktop commands to window actions."""

    def dispatch(name: str, arg) -> None:
        fn = handlers.get(name)
        if fn is None:
            log.debug("MPRIS command %s not handled", name)
            return
        fn() if arg is None else fn(arg)

    mpris.command.connect(dispatch)
