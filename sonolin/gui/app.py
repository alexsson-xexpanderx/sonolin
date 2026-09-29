"""The desktop front end: PyQt6 widgets, no QML.

Pick a speaker on the left, see and drive what it is playing in the card at the
top, and pick everything else from the menu above the speakers.

Nothing here talks to a speaker directly. Every call goes through `Controller`
or the `Speaker` facade and runs on the thread pool via `workers.run`, because
each one is a blocking HTTP request to a device on wifi. Results come back on
the GUI thread as signals, so the window never waits on the network.

State reaches the window two ways. Speakers push GENA events when something
changes, which trigger an immediate refresh; and a slow poll every few seconds
catches anything the events miss (a firewall can block them entirely). Between
polls the play position is advanced locally.
"""

from __future__ import annotations

import logging
import sys
import time
import urllib.request
from pathlib import Path

from PyQt6.QtCore import QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QKeySequence, QPainter, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QHBoxLayout, QInputDialog, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton,
    QSlider, QSplitter, QStackedWidget, QStatusBar, QStyle,
    QStyledItemDelegate, QToolButton, QVBoxLayout, QWidget,
)

from ..controller import Controller
from ..speaker import Speaker
from ..tags import Tags
from . import style, themes, workers
from .mpris import Mpris
from .mpris import connect as connect_mpris
from ..services import Services, service_track
from .browser import Browser
from .nav import NavList
from .nav import icon as nav_icon
from .panels import (
    AlarmsTab, AnnouncePanel, DevicePanel, FavouritesTab, LibraryTab, LinkDialog,
    NowPlaying, QueueTab, SoundPanel, StreamPanel,
)

log = logging.getLogger(__name__)

#: The pages, as the side bar lists them: (heading, [(name, icon), …]).
PAGES = [
    ("MUSIC", [("Queue", "queue"), ("Library", "library"), ("Browse", "browse"),
               ("Favourites", "favourites")]),
    ("SPEAKER", [("Sound", "sound"), ("Alarms", "alarms"), ("Announce", "announce"),
                 ("Stream desktop", "stream"), ("Device", "device")]),
]

TICK_MS = 1000
#: A full poll every this many ticks; the position is interpolated in between.
POLL_EVERY = 5
#: How long to leave a sleeping speaker before checking on it again.
ASLEEP_RETRY_S = 10

#: Speaker state -> the theme colour its dot is drawn in. Looked up when
#: painting, so a theme change recolours the dots straight away.
STATE_ROLES = {
    "PLAYING": "good",
    "PAUSED_PLAYBACK": "warn",
    "TRANSITIONING": "accent",
    "STOPPED": "faint",
    "ASLEEP": "border",
}


def state_colour(state: str) -> str:
    return style.C[STATE_ROLES.get(state, "faint")]


#: Extra data carried by each speaker row, read by `SpeakerDelegate`.
SUBTITLE_ROLE = Qt.ItemDataRole.UserRole + 1
STATE_ROLE = Qt.ItemDataRole.UserRole + 2


class SpeakerDelegate(QStyledItemDelegate):
    """Draws a speaker as a card: state dot, room name, and a quieter status line.

    The stock delegate lays out one line of text and elides the rest, which
    loses the model and state — the part that says whether a speaker is asleep.
    """

    HEIGHT = 58

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), self.HEIGHT)

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect).adjusted(2, 3, -2, -3)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(style.C["accent_lo"] if selected else style.C["raised"]))
            painter.drawRoundedRect(rect, 10, 10)

        colour = QColor(state_colour(index.data(STATE_ROLE) or ""))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        painter.drawEllipse(QRectF(rect.left() + 12, rect.center().y() - 5, 10, 10))

        text_left = rect.left() + 34
        width = rect.width() - 44
        name_font = QFont(option.font); name_font.setBold(True); name_font.setPointSizeF(10.5)
        sub_font = QFont(option.font); sub_font.setPointSizeF(8.8)

        painter.setFont(name_font)
        painter.setPen(QColor(style.C["selection_text" if selected else "text"]))
        name = painter.fontMetrics().elidedText(
            index.data(Qt.ItemDataRole.DisplayRole) or "", Qt.TextElideMode.ElideRight, int(width))
        painter.drawText(QRectF(text_left, rect.top() + 9, width, 20),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        painter.setFont(sub_font)
        sub = QColor(style.C["selection_text"])
        sub.setAlphaF(0.72)
        painter.setPen(sub if selected else QColor(style.C["muted"]))
        sub = painter.fontMetrics().elidedText(
            index.data(SUBTITLE_ROLE) or "", Qt.TextElideMode.ElideRight, int(width))
        painter.drawText(QRectF(text_left, rect.top() + 29, width, 18),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sub)
        painter.restore()


class MainWindow(QMainWindow):
    """The whole application."""

    #: (uid, kind, variables) from a speaker's GENA event, on SoCo's thread.
    speaker_event = pyqtSignal(str, str, dict)

    def __init__(self, controller: Controller) -> None:
        super().__init__()
        self.c = controller
        self.cfg = controller.config
        self.current: Speaker | None = None
        self._art_key: str | None = None
        self._polling = False
        self._ticks = 0
        self._next_wake_check = 0.0
        self._states: dict[str, str] = {}
        self._groups: dict[str, str] = {}

        self.setWindowTitle("Sonolin")
        self.resize(1260, 820)
        self.setMinimumSize(QSize(980, 640))
        self.speaker_event.connect(self._on_speaker_event)

        self._build_actions()
        self.setCentralWidget(self._build_body())
        self._build_statusbar()
        self._show_mode()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(TICK_MS)

        # Media keys, the desktop's media widget and the lock screen.
        self.mpris = Mpris(self)
        if self.mpris.register():
            connect_mpris(self, self.mpris, {
                "play": lambda: self._call("play"),
                "pause": lambda: self._call("pause"),
                "playpause": self._toggle_play,
                "stop": lambda: self._call("stop"),
                "next": lambda: self._call("next"),
                "previous": lambda: self._call("previous"),
                "seek": self._seek,
                "volume": self._mpris_volume,
                "shuffle": lambda on: self._set_play_mode(
                    bool(on), getattr(self, "_play_mode", (False, False))[1]),
                "loop": lambda status: self._set_play_mode(
                    getattr(self, "_play_mode", (False, False))[0],
                    {"Track": "ONE", "Playlist": True}.get(status, False)),
                "open": lambda uri: self._call("play_uri", uri),
            })
            self.mpris.raise_requested.connect(self._bring_to_front)
            self.mpris.quit_requested.connect(self.close)

        QTimer.singleShot(100, self._discover)
        QTimer.singleShot(200, self._scan)

    # -- construction ------------------------------------------------------

    def _build_body(self) -> QWidget:
        # side bar
        sidebar = QWidget(); sidebar.setObjectName("sidebar")
        sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        sidebar.setMinimumWidth(230); sidebar.setMaximumWidth(320)
        self.nav = NavList(PAGES)
        title = QLabel("SPEAKERS"); title.setObjectName("sidebarTitle")
        self.speakers = QListWidget(); self.speakers.setObjectName("speakerList")
        self.speakers.setItemDelegate(SpeakerDelegate(self.speakers))
        self.speakers.setMouseTracking(True)
        self.speakers.currentRowChanged.connect(self._speaker_changed)
        self.speakers.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.speakers.customContextMenuRequested.connect(self._speaker_menu)
        # The foot of the side bar: settings, and the light/dark switch.
        self.settings_btn = QPushButton("Settings"); self.settings_btn.setObjectName("sidebarButton")
        self.settings_btn.setToolTip("Find or add speakers, grouping, themes (Ctrl+,)")
        self.settings_btn.clicked.connect(self._open_settings)
        self.mode_btn = QToolButton(); self.mode_btn.setObjectName("sidebarButton")
        self.mode_btn.setDefaultAction(self.mode_action)
        self.mode_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        slay = QVBoxLayout(sidebar)
        slay.setContentsMargins(0, 0, 0, 12)
        slay.setSpacing(0)
        slay.addWidget(self.nav)
        slay.addWidget(title)
        slay.addWidget(self.speakers, 1)
        inner = QHBoxLayout(); inner.setContentsMargins(8, 6, 8, 0)
        inner.addWidget(self.settings_btn, 1); inner.addWidget(self.mode_btn)
        slay.addLayout(inner)

        # now playing card + transport
        self.now = NowPlaying()
        self.now.seek_requested.connect(self._seek)

        transport = QWidget(); transport.setObjectName("transport")
        tl = QHBoxLayout(transport); tl.setContentsMargins(4, 10, 4, 4); tl.setSpacing(10)
        prev = QPushButton("⏮"); prev.setToolTip("Previous")
        prev.clicked.connect(lambda: self._call("previous"))
        self.play_btn = QPushButton("▶"); self.play_btn.setObjectName("playButton")
        self.play_btn.setToolTip("Play / pause")
        self.play_btn.clicked.connect(self._toggle_play)
        stop = QPushButton("⏹"); stop.setToolTip("Stop")
        stop.clicked.connect(lambda: self._call("stop"))
        nxt = QPushButton("⏭"); nxt.setToolTip("Next")
        nxt.clicked.connect(lambda: self._call("next"))
        self.shuffle_btn = QPushButton("⤮"); self.shuffle_btn.setToolTip("Shuffle")
        self.shuffle_btn.setCheckable(True)
        self.shuffle_btn.clicked.connect(self._toggle_shuffle)
        self.repeat_btn = QPushButton("↻"); self.repeat_btn.setToolTip("Repeat: off")
        self.repeat_btn.setCheckable(True)
        self.repeat_btn.clicked.connect(self._cycle_repeat)
        for w in (self.shuffle_btn, prev, self.play_btn, stop, nxt, self.repeat_btn):
            tl.addWidget(w)
        tl.addSpacing(24)
        self.mute = QCheckBox("Mute")
        self.mute.toggled.connect(self._mute_changed)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100); self.volume.setMinimumWidth(180)
        self.volume.setMaximumWidth(280)
        self.volume.sliderReleased.connect(self._volume_changed)
        self.volume.valueChanged.connect(lambda v: self.vol_label.setText(str(v)))
        self.vol_label = QLabel("—"); self.vol_label.setObjectName("timeLabel")
        self.group_vol = QCheckBox("Whole group")
        self.group_vol.setToolTip("Move every speaker in the group together, keeping their balance")
        self.group_vol.toggled.connect(lambda _v: self._poll(force=True))
        self.volume.setToolTip("Volume")
        tl.addWidget(self.volume); tl.addWidget(self.vol_label)
        tl.addWidget(self.mute); tl.addWidget(self.group_vol)
        tl.addStretch(1)

        upper = QWidget(); ul = QVBoxLayout(upper)
        ul.setContentsMargins(16, 16, 16, 4)
        ul.addWidget(self.now); ul.addWidget(transport)

        # pages
        self.queue = QueueTab()
        self.queue.play_index.connect(
            lambda row: self._call("play_from_queue", row, soco=True, then=self._dirty))
        self.queue.remove_index.connect(
            lambda row: self._call("remove_from_queue", row, soco=True,
                                   then=self._refresh_queue))
        self.queue.clear_queue.connect(self._clear_queue)
        self.queue.move.connect(
            lambda row, step: self._run(
                self.c.move_in_queue, self.current, row + 1,
                row + 1 + (2 if step > 0 else -1), then=self._refresh_queue))
        self.queue.save.connect(self._save_queue)

        self.library = LibraryTab()
        self.library.play_tracks.connect(self._play_local)
        self.library.enqueue.connect(self._enqueue_local)
        self.library.rescan.connect(self._scan)
        self.library.add_folder.connect(self._add_folder)

        self.favourites = FavouritesTab()
        self.favourites.play_favourite.connect(
            lambda fav: self._run(self.c.play_favourite, self.current, fav,
                                  ok=f"Playing {fav.title}", then=self._dirty))
        self.favourites.play_playlist.connect(
            lambda pl: self._run(self.c.play_playlist, self.current, pl,
                                 ok=f"Playing {pl.title}", then=self._dirty))
        self.favourites.reload.connect(self._load_favourites)

        self.services = Services()
        self.browser = Browser(self.services, lambda: self.current)
        self.queue.set_art_loader(self.browser.loader)  # one artwork cache for both
        self.browser.loader.resolver = self._cover_lookup
        self.browser.status.connect(self._status)
        self.browser.error.connect(self._error)
        self.browser.played.connect(lambda: (self._dirty(), self._refresh_queue()))
        self.browser.link_requested.connect(self._service_link)
        self.browser.playlists_changed.connect(self._load_favourites)
        self.browser.service_changed.connect(lambda n: setattr(self.cfg, "last_service", n))
        self._services_loaded = False

        self.alarms = AlarmsTab()
        self.alarms.create.connect(self._create_alarm)
        self.alarms.toggle.connect(
            lambda a, on: self._run(self.c.set_alarm_enabled, a, on,
                                    ok="Alarm updated.", then=self._load_alarms))
        self.alarms.delete.connect(self._delete_alarm)
        self.alarms.reload.connect(self._load_alarms)

        self.sound = SoundPanel()
        self.sound.changed.connect(self._sound_changed)

        self.announce = AnnouncePanel(self._voices())
        idx = self.announce.voice.findData(self.cfg.tts_voice)
        if idx >= 0:
            self.announce.voice.setCurrentIndex(idx)
        self.announce.vol.setValue(self.cfg.announce_volume)
        self.announce.speak.connect(self._speak)
        self.announce.chime.connect(self._chime)
        self.announce.notify.connect(self._notify)

        self.stream = StreamPanel()
        self.stream.fmt.setCurrentText(self.cfg.stream_format)
        self.stream.start.connect(self._stream_start)
        self.stream.stop.connect(self._stream_stop)

        self.device = DevicePanel()
        self.device.rename.connect(self._rename)
        self.device.line_in.connect(lambda: self._call("switch_to_line_in", then=self._dirty))
        self.device.tv.connect(lambda: self._call("switch_to_tv", then=self._dirty))
        self.device.set_led.connect(
            lambda on: self._run(setattr, self.current.soco, "status_light", on,
                                 ok=f"Status light {'on' if on else 'off'}."))
        self.device.set_buttons.connect(
            lambda on: self._run(setattr, self.current.soco, "buttons_enabled", on,
                                 ok=f"Touch controls {'enabled' if on else 'locked'}."))
        self.device.sleep.connect(self._sleep)
        self.device.forget.connect(self._forget)

        # In the order `PAGES` lists them.
        self.pages = QStackedWidget()
        for widget in (self.queue, self.library, self.browser, self.favourites,
                       self.sound, self.alarms, self.announce, self.stream, self.device):
            self.pages.addWidget(widget)
        self.pages.currentChanged.connect(self._page_changed)
        self.nav.page_chosen.connect(self.pages.setCurrentIndex)
        self.nav.set_page(0)
        for n in range(self.pages.count()):  # Ctrl+1 … Ctrl+9
            QShortcut(QKeySequence(f"Ctrl+{n + 1}"), self,
                      activated=lambda n=n: self.nav.set_page(n))
        pane = QFrame(); pane.setObjectName("pagePane")
        pl = QVBoxLayout(pane); pl.setContentsMargins(10, 10, 10, 10)
        pl.addWidget(self.pages)
        lower = QWidget(); ll = QVBoxLayout(lower)
        ll.setContentsMargins(16, 8, 16, 12)
        ll.addWidget(pane)

        centre = QSplitter(Qt.Orientation.Vertical)
        centre.addWidget(upper); centre.addWidget(lower)
        centre.setSizes([300, 520])
        centre.setChildrenCollapsible(False)
        self.centre, self.upper = centre, upper

        outer = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(sidebar); outer.addWidget(centre)
        outer.setSizes([250, 1010])
        outer.setChildrenCollapsible(False)
        return outer

    def _build_actions(self) -> None:
        self.mode_action = QAction(self)
        self.mode_action.triggered.connect(self._toggle_mode)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=self._open_settings)
        QShortcut(QKeySequence("F5"), self, activated=self._discover)

    def _build_statusbar(self) -> None:
        self.setStatusBar(QStatusBar())
        self.busy = QProgressBar(); self.busy.setRange(0, 0)
        self.busy.setMaximumWidth(120); self.busy.hide()
        self.statusBar().addPermanentWidget(self.busy)
        # Shown only while the media server runs: it starts by itself the first
        # time something on this computer is played, and "off" is nothing to act on.
        self.server_label = QLabel(); self.server_label.setObjectName("hint")
        self.server_label.setToolTip(
            "Speakers fetch music files, announcements and the desktop stream\n"
            "from this computer at this address.")
        self.server_label.hide()
        self.statusBar().addPermanentWidget(self.server_label)

    # -- small helpers -----------------------------------------------------

    def _voices(self) -> list[tuple[str, str]]:
        from .. import tts

        try:
            return tts.voices()
        except Exception:
            return []

    def _status(self, text: str, busy: bool = False) -> None:
        self.statusBar().showMessage(text, 0 if busy else 8000)
        self.busy.setVisible(busy)

    def _error(self, message: str) -> None:
        self.busy.hide()
        self._status(f"⚠  {message}")
        log.warning("%s", message)

    def _run(self, fn, *args, ok: str | None = None, then=None, **kwargs) -> None:
        """Run `fn` in the background, report, and optionally follow up."""
        if self.current is None and args and args[0] is None:
            self._status("Pick a speaker first.")
            return

        def done(_result) -> None:
            self.busy.hide()
            if ok:
                self._status(ok)
            if then:
                then()

        workers.run(fn, *args, on_done=done, on_error=self._error, **kwargs)

    def _call(self, method: str, *args, soco: bool = False, then=None) -> None:
        """Invoke a method on the selected speaker, off the GUI thread."""
        sp = self.current
        if sp is None:
            self._status("Pick a speaker first.")
            return
        target = getattr(sp.soco if soco else sp, method)
        self._run(target, *args, then=then or self._dirty)

    def _dirty(self) -> None:
        """Something changed on purpose; refresh now rather than on the next poll."""
        self._poll(force=True)

    def _update_server_label(self) -> None:
        url = self.c.server_url
        self.server_label.setText(f"Sharing from this computer at {url}" if url else "")
        self.server_label.setVisible(bool(url))

    # -- discovery and the speaker list ------------------------------------

    def _discover(self) -> None:
        self._status("Looking for speakers…", busy=True)

        def job():
            found = self.c.refresh()
            states, groups = {}, {}
            for sp in found:
                if not sp.awake:
                    states[sp.uid] = "ASLEEP"
                    continue
                try:
                    states[sp.uid] = sp.state
                    members = [m.player_name for m in sp.soco.group.members
                               if m.is_visible and m.uid != sp.uid]
                    groups[sp.uid] = f"with {', '.join(sorted(members))}" if members else ""
                except Exception:
                    states[sp.uid] = "ASLEEP"
                    sp.awake = False
            return found, states, groups

        workers.run(job, on_done=self._speakers_found, on_error=self._error)

    def _speakers_found(self, result) -> None:
        found, self._states, self._groups = result
        self.busy.hide()
        keep = self.current.uid if self.current else self.cfg.last_speaker
        self.speakers.blockSignals(True)
        self.speakers.clear()
        for sp in found:
            self.speakers.addItem(self._speaker_item(sp))
        self.speakers.blockSignals(False)
        if not found:
            self.current = None
            self.now.title.setText("No speakers found")
            self.now.artist.setText("Check this machine and the speakers share a network,")
            self.now.album.setText("and that the firewall lets SSDP (UDP 1900) through.")
            self._status("No speakers found. Add one by address if discovery is blocked.")
            return
        row = next((i for i, sp in enumerate(found) if sp.uid == keep), 0)
        self.speakers.blockSignals(True)
        self.speakers.setCurrentRow(row)
        self.speakers.blockSignals(False)
        self._speaker_changed(row)
        awake = sum(1 for sp in found if sp.awake)
        self._status(f"{len(found)} speaker(s), {awake} awake.")

    def _speaker_item(self, sp: Speaker) -> QListWidgetItem:
        state = self._states.get(sp.uid, "STOPPED" if sp.awake else "ASLEEP")
        words = {"PLAYING": "playing", "PAUSED_PLAYBACK": "paused", "STOPPED": "idle",
                 "TRANSITIONING": "buffering", "ASLEEP": "asleep"}.get(state, state.lower())
        group = self._groups.get(sp.uid, "")
        item = QListWidgetItem(sp.name)
        item.setData(SUBTITLE_ROLE, f"{sp.model} · {words}{f' · {group}' if group else ''}")
        item.setData(STATE_ROLE, state)
        item.setToolTip(f"{sp.name}\n{sp.ip}\n{sp.uid}")
        return item

    def _refresh_item(self) -> None:
        row = self.speakers.currentRow()
        if self.current is not None and 0 <= row < self.speakers.count():
            fresh = self._speaker_item(self.current)
            item = self.speakers.item(row)
            item.setText(fresh.text())
            item.setData(SUBTITLE_ROLE, fresh.data(SUBTITLE_ROLE))
            item.setData(STATE_ROLE, fresh.data(STATE_ROLE))

    def _speaker_changed(self, row: int) -> None:
        if not (0 <= row < len(self.c.speakers)):
            self.current = None
            return
        previous = self.current
        self.current = self.c.speakers[row]
        if previous is not None and previous is not self.current:
            workers.run(self.c.unwatch, previous, on_error=lambda _e: None)
        self.cfg.last_speaker = self.current.uid
        self._art_key = None
        self._load_speaker()

    def _load_speaker(self) -> None:
        """Everything that depends on which speaker is selected."""
        sp = self.current
        if sp is None:
            return
        if not sp.awake:
            self.now.set_asleep(sp.name)
            self._next_wake_check = time.monotonic() + ASLEEP_RETRY_S
            return
        self.sound.load({})
        workers.run(sp.sound, on_done=self.sound.load, on_error=self._error)
        workers.run(lambda: (sp.status(), sp.capabilities),
                    on_done=lambda r: self.device.load(*r),
                    on_error=lambda _e: workers.run(sp.status, on_done=self.device.load,
                                                    on_error=self._error))
        workers.run(self.c.capture_sources,
                    on_done=lambda s: self.stream.set_sources(s, self.cfg.capture_source or None),
                    on_error=self._error)
        self._refresh_queue()
        self._page_changed(self.pages.currentIndex())
        # Bind the uid now: an event from a speaker the user has since moved away
        # from must not be applied to whichever speaker is selected when it lands.
        workers.run(self.c.watch, sp,
                    lambda kind, variables, uid=sp.uid: self.speaker_event.emit(
                        uid, kind, variables),
                    on_error=lambda e: log.info("events unavailable, polling only: %s", e))
        self._poll(force=True)

    def _speaker_menu(self, pos) -> None:
        item = self.speakers.itemAt(pos)
        if item is None:
            return
        row = self.speakers.row(item)
        sp = self.c.speakers[row]
        others = [o for o in self.c.speakers if o is not sp and o.awake]
        menu = QMenu(self)
        if sp.awake:
            join = menu.addMenu("Join the group of")
            join.setEnabled(bool(others))
            for other in others:
                join.addAction(other.name).triggered.connect(
                    lambda _c=False, a=sp, b=other: self._run(
                        a.join, b, ok=f"{a.name} joined {b.name}.", then=self._discover))
            menu.addAction("Leave its group").triggered.connect(
                lambda: self._run(sp.unjoin, ok=f"{sp.name} is on its own.",
                                  then=self._discover))
            menu.addAction("Group everything here").triggered.connect(
                lambda: self._run(sp.party_mode, ok="Everything grouped.",
                                  then=self._discover))
            menu.addSeparator()
            pair = menu.addMenu("Make a stereo pair with")
            twins = [o for o in others if o.model == sp.model]
            pair.setEnabled(bool(twins))
            for other in twins:
                pair.addAction(other.name).triggered.connect(
                    lambda _c=False, a=sp, b=other: self._stereo_pair(a, b))
            menu.addAction("Separate stereo pair").triggered.connect(
                lambda: self._run(sp.separate_stereo_pair, ok="Pair separated.",
                                  then=self._discover))
            menu.addSeparator()
        menu.addAction("Forget this speaker").triggered.connect(
            lambda: self._forget(sp))
        menu.exec(self.speakers.mapToGlobal(pos))

    def _stereo_pair(self, left: Speaker, right: Speaker) -> None:
        answer = QMessageBox.question(
            self, "Make a stereo pair",
            f"Bond {left.name} (left) and {right.name} (right) into one stereo pair?\n\n"
            "They will act as a single room until separated. Trueplay tuning on both "
            "is reset by pairing.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._run(left.create_stereo_pair, right, ok="Stereo pair created.",
                      then=self._discover)

    def _add_by_ip(self) -> None:
        ip, ok = QInputDialog.getText(
            self, "Add a speaker",
            "Speaker address (for when discovery cannot see it):",
            text="192.168.1.",
        )
        if not ok or not ip.strip():
            return
        self._status(f"Checking {ip.strip()}…", busy=True)
        self._run(self.c.add_by_ip, ip.strip(), ok=f"Added {ip.strip()}.",
                  then=self._discover)

    def _forget(self, sp: Speaker | None = None) -> None:
        sp = sp or self.current
        if sp is None:
            return
        answer = QMessageBox.question(
            self, "Forget speaker",
            f"Forget {sp.name}? It will come back the next time discovery finds it awake.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.c.forget(sp)
            if sp is self.current:
                self.current = None
            self._discover()

    # -- live state --------------------------------------------------------

    def _on_speaker_event(self, uid: str, kind: str, variables: dict) -> None:
        if self.current is None or uid != self.current.uid:
            return
        if kind == "content" and any(
            str(v).startswith("Q:0") for v in variables.values()
        ):
            self._refresh_queue()
        elif kind == "topology":
            self._discover()
            return
        self._poll(force=True)

    def _bring_to_front(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _mpris_volume(self, fraction: float) -> None:
        value = round(fraction * 100)
        self.volume.setValue(value)
        self._volume_changed()

    def _tick(self) -> None:
        self._ticks += 1
        self.now.tick(TICK_MS / 1000)
        self.mpris.advance(TICK_MS / 1000)
        if self._ticks % POLL_EVERY == 0:
            self._poll()

    def _poll(self, force: bool = False) -> None:
        sp = self.current
        if sp is None or self._polling:
            return
        if not sp.awake and not force and time.monotonic() < self._next_wake_check:
            return
        self._polling = True
        with_sleep = self._ticks % (POLL_EVERY * 3) == 0 or force
        group = self.group_vol.isChecked()

        def gather() -> dict:
            if not sp.awake and not sp.reachable():
                return {"asleep": True}
            try:
                data = {
                    "track": sp.track,
                    "state": sp.state,
                    "volume": sp.group_volume if group else sp.volume,
                    "mute": sp.soco.mute,
                    "play_mode": sp.soco.play_mode,
                }
                if with_sleep:
                    data["sleep"] = sp.soco.get_sleep_timer()
                was_asleep = not sp.awake
                sp.awake = True
                data["woke"] = was_asleep
                return data
            except Exception:
                if not sp.reachable():
                    return {"asleep": True}
                raise

        workers.run(gather, on_done=self._apply_poll, on_error=self._poll_failed)

    def _poll_failed(self, message: str) -> None:
        self._polling = False
        log.debug("poll failed: %s", message)

    def _apply_poll(self, data: dict) -> None:
        self._polling = False
        sp = self.current
        if sp is None:
            return
        if data.get("asleep"):
            self._states[sp.uid] = "ASLEEP"
            self.now.set_asleep(sp.name)
            self.mpris.update(state=None, title=f"{sp.name} is asleep", has_speaker=False)
            self.queue.set_playing(0, "", "")
            self.play_btn.setText("▶")
            self._next_wake_check = time.monotonic() + ASLEEP_RETRY_S
            self._refresh_item()
            return
        if data.get("woke"):
            self._status(f"{sp.name} is awake again.")
            self._load_speaker()
        info = data["track"]
        state = data["state"]
        self._states[sp.uid] = state
        self.browser.set_now_playing(info.get("uri") or "")
        try:
            position = int(info.get("playlist_position") or 0)
        except ValueError:
            position = 0
        self.queue.set_playing(position, info.get("uri") or "", state)
        source = "desktop" if "/stream/live." in (info.get("uri") or "") else ""
        self.now.set_track(info, state, source)
        self.play_btn.setText("⏸" if state in ("PLAYING", "TRANSITIONING") else "▶")
        if not self.volume.isSliderDown():
            self.volume.blockSignals(True)
            self.volume.setValue(int(data["volume"]))
            self.volume.blockSignals(False)
            self.vol_label.setText(str(data["volume"]))
        self.mute.blockSignals(True)
        self.mute.setChecked(bool(data["mute"]))
        self.mute.blockSignals(False)
        if "sleep" in data:
            self.device.set_sleep_remaining(data["sleep"])
        self._show_play_mode(data.get("play_mode", "NORMAL"))
        shuffle, repeat = self._play_mode
        self.mpris.update(
            state=state,
            title=self.now.title.text() if self.now.title.text() != "Nothing playing" else "",
            artist=info.get("artist") or "", album=info.get("album") or "",
            art=info.get("album_art") or "", uri=info.get("uri") or "",
            length=self.now._duration, position=self.now._elapsed,
            volume=int(data["volume"]), shuffle=shuffle, repeat=repeat,
        )
        art = info.get("album_art") or ""
        if art and art != self._art_key:
            self._art_key = art
            workers.run(self._fetch_art, art, on_done=self.now.set_art,
                        on_error=lambda _e: None)
        elif not art:
            self._art_key = None
            self.now.set_art(None)
        self._refresh_item()

    @staticmethod
    def _fetch_art(url: str) -> bytes | None:
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                return r.read(4_000_000)
        except Exception:
            return None

    # -- transport and volume ---------------------------------------------

    def _toggle_play(self) -> None:
        state = self._states.get(self.current.uid, "") if self.current else ""
        self._call("pause" if state in ("PLAYING", "TRANSITIONING") else "play")

    #: play mode -> (shuffle, repeat) where repeat is False, True or "ONE"
    PLAY_MODES = {
        "NORMAL": (False, False), "REPEAT_ALL": (False, True),
        "REPEAT_ONE": (False, "ONE"), "SHUFFLE_NOREPEAT": (True, False),
        "SHUFFLE": (True, True), "SHUFFLE_REPEAT_ONE": (True, "ONE"),
    }

    def _show_play_mode(self, mode: str) -> None:
        shuffle, repeat = self.PLAY_MODES.get(mode, (False, False))
        self._play_mode = (shuffle, repeat)
        self.shuffle_btn.setChecked(shuffle)
        self.repeat_btn.setChecked(bool(repeat))
        self.repeat_btn.setText("↻¹" if repeat == "ONE" else "↻")
        self.repeat_btn.setToolTip(
            {False: "Repeat: off", True: "Repeat: all", "ONE": "Repeat: this track"}[repeat])

    def _set_play_mode(self, shuffle: bool, repeat) -> None:
        mode = next(m for m, v in self.PLAY_MODES.items() if v == (shuffle, repeat))
        self._show_play_mode(mode)
        self._run(setattr, self.current.soco, "play_mode", mode, then=self._dirty)

    def _toggle_shuffle(self) -> None:
        if self.current is not None:
            shuffle, repeat = getattr(self, "_play_mode", (False, False))
            self._set_play_mode(not shuffle, repeat)

    def _cycle_repeat(self) -> None:
        if self.current is not None:
            shuffle, repeat = getattr(self, "_play_mode", (False, False))
            self._set_play_mode(shuffle, {False: True, True: "ONE", "ONE": False}[repeat])

    def _save_queue(self) -> None:
        if self.current is None:
            return
        title, ok = QInputDialog.getText(self, "Save queue", "Playlist name:")
        if ok and title.strip():
            self._run(self.c.save_queue, self.current, title.strip(),
                      ok=f"Saved as “{title.strip()}”.")

    def _seek(self, seconds: float) -> None:
        # UPnP REL_TIME wants H:MM:SS even for short tracks.
        total = int(seconds)
        self._call("seek", f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}")

    def _volume_changed(self) -> None:
        sp = self.current
        if sp is None:
            return
        value = self.volume.value()
        attr = "group_volume" if self.group_vol.isChecked() else "volume"
        workers.run(setattr, sp, attr, value, on_error=self._error)

    def _mute_changed(self, on: bool) -> None:
        sp = self.current
        if sp is not None:
            workers.run(setattr, sp.soco, "mute", on, on_error=self._error)

    def _sound_changed(self, key: str, value: object) -> None:
        sp = self.current
        if sp is None:
            return
        if key == "balance":
            # -100 is hard left, +100 hard right; Sonos wants a (left, right) pair
            # of channel levels, each 0-100.
            offset = int(value)
            value = (100 - max(0, offset), 100 - max(0, -offset))
        self._run(setattr, sp.soco, key, value,
                  ok=f"{key.replace('_', ' ').capitalize()} set.")

    # -- pages that load lazily --------------------------------------------

    def _page_changed(self, index: int) -> None:
        widget = self.pages.widget(index)
        # Browsing needs the height; the now-playing card gives it up meanwhile.
        compact = widget in (self.browser, self.library)
        if compact != self.now.compact:
            self.now.set_compact(compact)

            def resize() -> None:
                # Measured a turn later: the card's new minimum only exists once
                # Qt has re-laid it out.
                total = sum(self.centre.sizes())
                upper = self.upper.minimumSizeHint().height() if compact else 300
                self.centre.setSizes([upper, max(200, total - upper)])

            QTimer.singleShot(0, resize)
        if widget is self.favourites:
            self._load_favourites()
        elif widget is self.browser and not self._services_loaded:
            self._load_services()
        elif widget is self.alarms:
            self._load_alarms()
        elif widget is self.queue:
            self._refresh_queue()

    def _cover_lookup(self, url: str):
        """How to ask a song's service for its cover, or None if it has none.

        Given to the artwork loader: a queued song's cover would otherwise come
        from the speaker, which makes them one at a time.
        """
        sp = self.current
        if sp is None or service_track(url) is None:
            return None
        return lambda: self.services.cover_for(sp, url)

    def _refresh_queue(self) -> None:
        sp = self.current
        if sp is None or not sp.awake:
            return
        def job():
            items = sp.soco.get_queue(max_items=1000)
            # Service tracks name their cover relative to the speaker
            # ("/getaa?…"); local ones already carry a full address.
            library = sp.soco.music_library
            arts = [library.build_album_art_full_uri(i.album_art_uri)
                    if getattr(i, "album_art_uri", None) else "" for i in items]
            return items, arts

        workers.run(job, on_done=lambda r: self.queue.load(*r), on_error=self._error)

    def _clear_queue(self) -> None:
        if self.current is None:
            return
        answer = QMessageBox.question(
            self, "Clear queue", f"Remove everything from {self.current.name}'s queue?"
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._call("clear_queue", soco=True, then=self._refresh_queue)

    def _load_favourites(self) -> None:
        sp = self.current
        if sp is None or not sp.awake:
            return
        workers.run(lambda: (self.c.favourites(sp), self.c.playlists(sp)),
                    on_done=lambda r: self.favourites.load(*r), on_error=self._error)

    def _load_alarms(self) -> None:
        sp = self.current
        if sp is None or not sp.awake:
            return
        workers.run(self.c.alarms, sp, on_done=self.alarms.load, on_error=self._error)

    def _create_alarm(self, values: dict) -> None:
        sp = self.current
        if sp is None:
            return
        self._run(self.c.create_alarm, sp, values["start"],
                  recurrence=values["recurrence"], volume=values["volume"],
                  duration=values["duration"],
                  ok=f"Alarm set for {values['start'].strftime('%H:%M')} in {sp.name}.",
                  then=self._load_alarms)

    def _delete_alarm(self, alarm) -> None:
        answer = QMessageBox.question(
            self, "Delete alarm", f"Delete the {alarm.start_time.strftime('%H:%M')} alarm?"
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._run(self.c.delete_alarm, alarm, ok="Alarm deleted.",
                      then=self._load_alarms)

    # -- music services ------------------------------------------------------

    def _load_services(self) -> None:
        sp = self.current
        if sp is None or not sp.awake:
            return

        def job():
            catalogue = self.services.catalogue()
            linked = {s.name for s in catalogue if self.services.is_linked(s.name, sp)}
            return catalogue, linked

        def done(result) -> None:
            self._services_loaded = True
            catalogue, linked = result
            self.browser.load_services(catalogue, linked, self.cfg.last_service)
            self.browser.refresh_playlists()

        workers.run(job, on_done=done, on_error=self._error)

    def _service_link(self, name: str) -> None:
        sp = self.current
        if sp is None:
            return

        def got_url(url: str) -> None:
            dialog = LinkDialog(name, url, self)
            if dialog.exec():
                self.cfg.last_service = name
                self._run(self.services.complete_link, name, ok=f"{name} linked.",
                          then=self._load_services)

        workers.run(self.services.begin_link, name, sp, on_done=got_url, on_error=self._error)

    # -- local library -----------------------------------------------------

    def _add_folder(self, folder: str) -> None:
        if Path(folder) not in self.c.library.roots:
            self.c.library.roots.append(Path(folder))
        self._scan()

    def _scan(self) -> None:
        roots = self.c.library.roots
        if not roots:
            self.library.stats.setText(
                "No music folder found. Use “Add folder…” to point at your music."
            )
            return
        self._status("Scanning the music library…", busy=True)
        workers.run(self.c.library.scan, on_done=self._scanned, on_error=self._error)

    def _scanned(self, count: int) -> None:
        self.busy.hide()
        self.library.load(self.c.library)
        self.c._save()
        roots = ", ".join(str(r) for r in self.c.library.roots)
        self._status(f"{count} tracks in {roots}")

    def _play_local(self, tracks: list[Tags], start: int) -> None:
        self._run(self.c.play_local_list, self.current, tracks, start,
                  ok=f"Playing {tracks[start].display_title}",
                  then=lambda: (self._update_server_label(), self._dirty(),
                                self._refresh_queue()))

    def _enqueue_local(self, tracks: list[Tags]) -> None:
        self._run(self.c.enqueue, self.current, tracks,
                  ok=f"Queued {len(tracks)} track(s).",
                  then=lambda: (self._update_server_label(), self._refresh_queue()))

    # -- announcements -----------------------------------------------------

    def _speak(self, text: str, voice: str, wpm: int, volume: object) -> None:
        self.cfg.tts_voice = voice
        if volume is not None:
            self.cfg.announce_volume = int(volume)
        self._status("Speaking…", busy=True)
        self._run(self.c.say, self.current, text, voice=voice, wpm=wpm, volume=volume,
                  ok="Announcement sent.", then=self._update_server_label)

    def _chime(self, volume: object) -> None:
        sp = self.current
        if sp is None:
            return
        self._run(sp.announce, clip_type="CHIME", volume=volume, ok="Chime sent.")

    def _notify(self, url: str, volume: object) -> None:
        if not url:
            self._status("Give a URL the speaker can reach.")
            return
        self._run(self.c.notify, self.current, url, volume=volume, ok="Sound sent.")

    # -- desktop streaming -------------------------------------------------

    def _stream_start(self, source: str, fmt: str) -> None:
        sp = self.current
        if sp is None:
            return
        self.cfg.stream_format = fmt
        self.cfg.capture_source = source
        self._status("Starting the stream…", busy=True)

        def do() -> str:
            self.c.set_capture_source(source or None)
            return self.c.stream_desktop(sp, fmt)

        def done(url: str) -> None:
            self.busy.hide()
            self.stream.status.setText(f"Streaming {source} as {fmt.upper()} to {sp.name}\n{url}")
            self._update_server_label()
            self._status("Streaming desktop audio.")
            self._dirty()

        workers.run(do, on_done=done, on_error=self._error)

    def _stream_stop(self) -> None:
        sp = self.current
        self._run(lambda: self.c.stop_desktop(sp),
                  then=lambda: self.stream.status.setText("Not streaming."))

    # -- device ------------------------------------------------------------

    def _rename(self, name: str) -> None:
        sp = self.current
        if sp is None or not name or name == sp.name:
            return

        def do() -> None:
            sp.soco.player_name = name
            sp._name = name

        self._run(do, ok=f"Renamed to {name}.", then=self._discover)

    def _sleep(self, seconds) -> None:
        sp = self.current
        if sp is None:
            return
        self._run(sp.sleep_timer, seconds,
                  ok="Sleep timer cancelled." if seconds is None
                  else f"Sleeping in {seconds // 60} min.",
                  then=self._dirty)

    # -- themes ------------------------------------------------------------

    def current_theme(self) -> themes.Theme:
        return themes.find(self.cfg.theme) or themes.builtin_themes()[0]

    def apply_theme(self, theme: themes.Theme, *, remember: bool = True) -> None:
        """Recolour the whole window now.

        The stylesheet and palette cover most widgets; the few things that
        paint themselves, or bake a colour into text, are asked to redo it.
        """
        style.apply(QApplication.instance(), theme.colors)
        if remember:
            self.cfg.theme = theme.name
            setattr(self.cfg, f"theme_{theme.base}", theme.name)
        self._show_mode(theme)
        self.browser.restyle()
        self.queue.restyle()
        for widget in QApplication.instance().allWidgets():
            widget.update()

    def _show_mode(self, theme: themes.Theme | None = None) -> None:
        """Label the light/dark switch, and tint the side bar's icons to match."""
        dark = (theme or self.current_theme()).base == "dark"
        self.mode_action.setText("Light mode" if dark else "Dark mode")
        self.mode_action.setToolTip(
            f"Switch to {'light' if dark else 'dark'} mode "
            f"({self.cfg.theme_light if dark else self.cfg.theme_dark})")
        muted = style.C["muted"]
        self.mode_action.setIcon(QIcon(nav_icon("sun" if dark else "moon", muted, 18)))
        self.settings_btn.setIcon(QIcon(nav_icon("settings", muted, 18)))

    def _toggle_mode(self) -> None:
        other = "light" if self.current_theme().base == "dark" else "dark"
        name = getattr(self.cfg, f"theme_{other}")
        target = themes.find(name)
        if target is None or target.base != other:  # renamed or deleted since
            target = next(t for t in themes.builtin_themes() if t.base == other)
        self.apply_theme(target)
        self.c._save()

    def _open_settings(self) -> None:
        from .settings_dialog import SettingsDialog

        SettingsDialog(self).exec()

    def _open_themes(self) -> None:
        from .theme_dialog import ThemeDialog

        dialog = ThemeDialog(self, self.current_theme())
        dialog.exec()
        self.c._save()

    # -- shutdown ----------------------------------------------------------

    def closeEvent(self, event) -> None:
        self.timer.stop()
        self.mpris.unregister()
        try:
            self.c._save()
            self.c.close()
        except Exception:
            log.debug("shutdown", exc_info=True)
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("sonolin")
    app.setApplicationDisplayName("Sonolin")
    app.setDesktopFileName("sonolin")  # ties windows to sonolin.desktop on Wayland
    app.setWindowIcon(QIcon(str(Path(__file__).with_name("sonolin.svg"))))
    controller = Controller()
    saved = themes.find(controller.config.theme) or themes.builtin_themes()[0]
    style.apply(app, saved.colors)
    window = MainWindow(controller)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
