"""The widgets inside the main window, each unaware of Sonos.

Panels take plain data in (`load`, `set_*`) and emit signals out. None of them
calls a speaker, so none of them can block; the window decides what each signal
means and runs it on the thread pool.
"""

from __future__ import annotations

import logging

import datetime
from types import SimpleNamespace

from PyQt6.QtCore import QEvent, QObject, QSize, Qt, QTime, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QColor, QDesktopServices, QFont, QFontDatabase, QFontMetrics, QIcon, QPixmap,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QSlider, QSpinBox, QSplitter, QStyle, QStyleOptionSlider,
    QTableWidget, QTableWidgetItem, QTextEdit, QTimeEdit, QVBoxLayout, QWidget,
)

from ..alarms import DAY_NAMES, RECURRENCES, describe_recurrence
from ..tags import Tags
from . import style

log = logging.getLogger(__name__)

def fmt_time(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_time(text: str) -> float:
    parts = [p for p in (text or "").split(":") if p != ""]
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return 0.0
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def frame_table(table: QTableWidget) -> None:
    """Fit a table's heading row neatly inside its rounded frame.

    Headings sit over the left edge of their column, where the cells start; Qt
    centres them by default, so a heading floats away from what it labels.

    The heading row is left unfilled (see the stylesheet), because Qt draws it
    as a plain rectangle over the frame's rounded corners. And Qt runs the
    scroll bar up beside the headings; a cap as tall as the heading row goes
    above it instead, carrying the heading row's bottom line on to the edge.
    """
    header = table.horizontalHeader()
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    cap = QWidget()
    cap.setObjectName("headerCap")
    cap.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    cap.setFixedHeight(header.sizeHint().height())
    table.addScrollBarWidget(cap, Qt.AlignmentFlag.AlignTop)
    _MatchHeight(header, cap)


class _MatchHeight(QObject):
    """Keeps `follower` as tall as `leader`, which a theme's font can change."""

    def __init__(self, leader: QWidget, follower: QWidget) -> None:
        super().__init__(follower)
        self.follower = follower
        leader.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Resize:
            self.follower.setFixedHeight(obj.height())
        return False


class JumpSlider(QSlider):
    """A slider that goes straight to where it is clicked.

    A plain QSlider moves one page towards the click, which on a volume or seek
    bar looks like the click did nothing. After the jump the handle is under the
    pointer, so the same press carries on as a drag.
    """

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            cc, style_ = QStyle.ComplexControl.CC_Slider, self.style()
            handle = style_.subControlRect(cc, opt, QStyle.SubControl.SC_SliderHandle, self)
            where = event.position().toPoint()
            if not handle.contains(where):
                groove = style_.subControlRect(cc, opt, QStyle.SubControl.SC_SliderGroove, self)
                horizontal = self.orientation() == Qt.Orientation.Horizontal
                length = handle.width() if horizontal else handle.height()
                start = groove.x() if horizontal else groove.y()
                span = (groove.width() if horizontal else groove.height()) - length
                offset = (where.x() if horizontal else where.y()) - start - length // 2
                self.setValue(QStyle.sliderValueFromPosition(
                    self.minimum(), self.maximum(), offset, span, opt.upsideDown))
        super().mousePressEvent(event)


class NowPlaying(QWidget):
    """Cover art, track text and position for the selected speaker."""

    seek_requested = pyqtSignal(float)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("nowPlaying")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._duration = 0.0
        self._elapsed = 0.0
        self._playing = False
        self._dragging = False

        self.art = QLabel("♪")
        self.art.setObjectName("coverArt")
        self.art.setFixedSize(QSize(176, 176))
        self.art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = QFont(); f.setPointSize(40)
        self.art.setFont(f)

        self.state = QLabel("")
        self.state.setObjectName("trackState")
        self.title = QLabel("Nothing playing")
        self.title.setObjectName("trackTitle")
        self.title.setWordWrap(True)
        self.artist = QLabel("")
        self.artist.setObjectName("trackArtist")
        self.album = QLabel("")
        self.album.setObjectName("trackAlbum")

        self.position = JumpSlider(Qt.Orientation.Horizontal)
        self.position.setRange(0, 1000)
        self.position.sliderPressed.connect(lambda: setattr(self, "_dragging", True))
        self.position.sliderReleased.connect(self._released)
        self.elapsed = QLabel("0:00"); self.elapsed.setObjectName("timeLabel")
        self.total = QLabel("0:00"); self.total.setObjectName("timeLabel")
        self.total.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        bar = QHBoxLayout()
        bar.addWidget(self.elapsed)
        bar.addWidget(self.position, 1)
        bar.addWidget(self.total)

        text = QVBoxLayout()
        text.setSpacing(4)
        text.addWidget(self.state)
        text.addWidget(self.title)
        text.addWidget(self.artist)
        text.addWidget(self.album)
        text.addStretch(1)
        text.addLayout(bar)

        root = QHBoxLayout(self)
        root.setContentsMargins(18, 18, 22, 18)
        root.setSpacing(22)
        root.addWidget(self.art)
        root.addLayout(text, 1)
        self._root = root
        self._art_data: bytes | None = None
        self.compact = False

    def set_compact(self, on: bool) -> None:
        """Shrink to a slim bar while a tab needs the room, e.g. browsing.

        Title, artist and position stay; the cover shrinks and the album line
        goes, since pages being browsed usually show the album anyway.
        """
        if on == self.compact:
            return
        self.compact = on
        size = 64 if on else 176
        self.art.setFixedSize(QSize(size, size))
        f = QFont(); f.setPointSize(18 if on else 40)
        self.art.setFont(f)
        self.album.setVisible(not on)
        self._root.setContentsMargins(*((12, 10, 18, 10) if on else (18, 18, 22, 18)))
        self._root.setSpacing(16 if on else 22)
        self.title.setProperty("compact", "true" if on else "false")
        self.title.style().unpolish(self.title)
        self.title.style().polish(self.title)
        self.set_art(self._art_data)

    def _released(self) -> None:
        self._dragging = False
        if self._duration > 0:
            self.seek_requested.emit(self.position.value() / 1000 * self._duration)

    def set_track(self, info: dict, state: str, source: str = "") -> None:
        title = info.get("title") or ""
        uri = info.get("uri") or ""
        if not title and uri:
            title = "Desktop audio" if "/stream/live." in uri else uri.rsplit("/", 1)[-1]
        self.title.setText(title or "Nothing playing")
        self.artist.setText(info.get("artist") or "")
        self.album.setText(info.get("album") or "")
        label = {"PLAYING": "PLAYING", "PAUSED_PLAYBACK": "PAUSED",
                 "STOPPED": "STOPPED", "TRANSITIONING": "BUFFERING"}.get(state, state)
        self.state.setText(f"{label}{f'  ·  {source.upper()}' if source else ''}")
        self._duration = parse_time(info.get("duration", ""))
        if self._duration:
            self.total.setText(fmt_time(self._duration))
        else:
            # Radio and the desktop stream have no length.
            self.total.setText("live" if state == "PLAYING" and uri else "0:00")
        self.position.setEnabled(self._duration > 0)
        self._playing = state == "PLAYING"
        self._elapsed = parse_time(info.get("position", ""))
        self._show_position()

    def _show_position(self) -> None:
        if self._dragging:
            return
        self.elapsed.setText(fmt_time(self._elapsed))
        self.position.setValue(
            int(min(self._elapsed, self._duration) / self._duration * 1000)
            if self._duration else 0
        )

    def tick(self, seconds: float = 1.0) -> None:
        """Advance the position locally between polls.

        Asking the speaker for its position every second costs a round trip per
        second for a number that moves predictably; interpolating and correcting
        on each real poll looks the same and costs a fifth of the traffic.
        """
        if getattr(self, "_playing", False) and self._duration:
            self._elapsed = min(self._elapsed + seconds, self._duration)
            self._show_position()

    def stop_moving(self) -> None:
        """Hold the position where it is until the speaker says otherwise."""
        self._playing = False

    def set_asleep(self, name: str) -> None:
        self._playing = False
        self.state.setText("ASLEEP")
        self.title.setText(f"{name} is not answering")
        self.artist.setText("Battery speakers drop off the network when idle.")
        self.album.setText("Press a button on it, or put it on its charger.")
        self.elapsed.setText("0:00"); self.total.setText("0:00")
        self.position.setValue(0); self.position.setEnabled(False)
        self.set_art(None)

    def set_art(self, data: bytes | None) -> None:
        self._art_data = data
        pix = QPixmap()
        if data and pix.loadFromData(data):
            self.art.setText("")
            self.art.setPixmap(
                pix.scaled(self.art.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
            )
        else:
            self.art.setPixmap(QPixmap())
            self.art.setText("♪")


class SoundPanel(QWidget):
    """Tone and spatial controls, hiding whatever the model lacks.

    Sonos models differ enormously here: a Move has bass, treble and loudness; a
    soundbar adds dialog enhancement, surround levels, sub crossover and audio
    delay. A control is shown only if the speaker answered when asked for it, so
    the panel matches the hardware instead of offering dead sliders.
    """

    changed = pyqtSignal(str, object)

    SLIDERS = (
        ("bass", "Bass", -10, 10),
        ("treble", "Treble", -10, 10),
        ("sub_gain", "Sub gain", -15, 15),
        ("surround_level", "Surround level", -15, 15),
        ("music_surround_level", "Surround (music)", -15, 15),
        ("audio_delay", "Audio delay", 0, 5),
    )
    TOGGLES = (
        ("loudness", "Loudness"),
        ("night_mode", "Night mode"),
        ("dialog_mode", "Speech enhancement"),
        ("cross_fade", "Crossfade"),
        ("sub_enabled", "Subwoofer"),
        ("surround_enabled", "Surround speakers"),
    )

    def __init__(self) -> None:
        super().__init__()
        self._widgets: dict[str, QWidget] = {}
        self._rows: dict[str, QWidget] = {}
        self._loading = False

        form = QFormLayout()
        for key, label, lo, hi in self.SLIDERS:
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(lo, hi)
            value = QLabel("0")
            value.setFixedWidth(28)
            slider.valueChanged.connect(lambda v, lbl=value: lbl.setText(str(v)))
            slider.sliderReleased.connect(
                lambda k=key, s=slider: self._emit(k, s.value())
            )
            row = QWidget()
            lay = QHBoxLayout(row); lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(slider, 1); lay.addWidget(value)
            self._widgets[key] = slider
            self._rows[key] = row
            form.addRow(label, row)

        for key, label in self.TOGGLES:
            box = QCheckBox()
            box.toggled.connect(lambda v, k=key: self._emit(k, v))
            self._widgets[key] = box
            self._rows[key] = box
            form.addRow(label, box)

        self.balance = QSlider(Qt.Orientation.Horizontal)
        self.balance.setRange(-100, 100)
        self.balance.sliderReleased.connect(
            lambda: self._emit("balance", self.balance.value())
        )
        self._widgets["balance"] = self.balance
        self._rows["balance"] = self.balance
        form.addRow("Balance (L–R)", self.balance)

        self.note = QLabel("Select a speaker.")
        self.note.setObjectName("hint")
        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(self.note)
        root.addStretch(1)

    def _emit(self, key: str, value: object) -> None:
        if not self._loading:
            self.changed.emit(key, value)

    def load(self, sound: dict) -> None:
        self._loading = True
        shown = 0
        for key, widget in self._widgets.items():
            value = sound.get(key)
            row = self._rows[key]
            supported = value is not None
            row.setVisible(supported)
            label_for = self.layout().itemAt(0).layout().labelForField(row)
            if label_for is not None:
                label_for.setVisible(supported)
            if not supported:
                continue
            shown += 1
            if key == "balance" and isinstance(value, (tuple, list)) and len(value) == 2:
                left, right = value
                widget.setValue(int(right) - int(left))
            elif isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QSlider):
                try:
                    widget.setValue(int(value))
                except (TypeError, ValueError):
                    row.setVisible(False)
                    shown -= 1
        self.note.setText(f"{shown} controls supported by this model.")
        self._loading = False


class AnnouncePanel(QWidget):
    """Speak text, or play a sound, over whatever is already playing."""

    speak = pyqtSignal(str, str, int, object)
    chime = pyqtSignal(object)
    notify = pyqtSignal(str, object)

    def __init__(self, voices: list[tuple[str, str]]) -> None:
        super().__init__()
        self.text = QTextEdit()
        self.text.setPlaceholderText("Dinner is ready.")
        self.text.setMaximumHeight(90)

        self.voice = QComboBox()
        for code, name in voices or [("en", "English")]:
            self.voice.addItem(f"{name} ({code})", code)
        idx = self.voice.findData("en")
        if idx >= 0:
            self.voice.setCurrentIndex(idx)

        self.wpm = QSpinBox(); self.wpm.setRange(80, 400); self.wpm.setValue(165)
        self.vol = QSpinBox(); self.vol.setRange(0, 100); self.vol.setValue(40)
        self.use_vol = QCheckBox("Set clip volume"); self.use_vol.setChecked(True)

        say_btn = QPushButton("Speak")
        say_btn.clicked.connect(self._say)
        chime_btn = QPushButton("Built-in chime")
        chime_btn.clicked.connect(lambda: self.chime.emit(self._volume()))

        self.url = QLineEdit()
        self.url.setPlaceholderText("http://host/doorbell.mp3")
        url_btn = QPushButton("Play sound")
        url_btn.clicked.connect(
            lambda: self.notify.emit(self.url.text().strip(), self._volume())
        )

        form = QFormLayout()
        form.addRow("Say", self.text)
        form.addRow("Voice", self.voice)
        form.addRow("Words / min", self.wpm)
        form.addRow(self.use_vol, self.vol)

        buttons = QHBoxLayout()
        buttons.addWidget(say_btn); buttons.addWidget(chime_btn); buttons.addStretch(1)

        url_row = QHBoxLayout()
        url_row.addWidget(self.url, 1); url_row.addWidget(url_btn)

        note = QLabel(
            "Announcements layer over the music: it ducks, the clip plays, it comes "
            "back. The queue is untouched. Needs a speaker reporting AUDIO_CLIP."
        )
        note.setWordWrap(True)
        note.setObjectName("hint")

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addLayout(buttons)
        root.addWidget(QLabel("Or play a sound from a URL the speaker can reach:"))
        root.addLayout(url_row)
        root.addWidget(note)
        root.addStretch(1)

    def _volume(self) -> int | None:
        return self.vol.value() if self.use_vol.isChecked() else None

    def _say(self) -> None:
        text = self.text.toPlainText().strip()
        if text:
            self.speak.emit(text, self.voice.currentData(), self.wpm.value(), self._volume())


class StreamPanel(QWidget):
    """Send this machine's own audio output to a speaker."""

    start = pyqtSignal(str, str)
    stop = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.source = QComboBox()
        self.fmt = QComboBox()
        self.fmt.addItems(["flac", "mp3", "wav"])

        start_btn = QPushButton("Stream desktop audio to this speaker")
        start_btn.clicked.connect(
            lambda: self.start.emit(self.source.currentText(), self.fmt.currentText())
        )
        stop_btn = QPushButton("Stop")
        stop_btn.clicked.connect(self.stop.emit)

        self.status = QLabel("Not streaming.")
        self.status.setWordWrap(True)

        note = QLabel(
            "Captures a sink monitor and serves it to the speaker as a live stream. "
            "Pick the monitor of the output you actually hear. FLAC is lossless; "
            "MP3 is the safest with older players."
        )
        note.setWordWrap(True)
        note.setObjectName("hint")

        form = QFormLayout()
        form.addRow("Capture source", self.source)
        form.addRow("Encoding", self.fmt)

        buttons = QHBoxLayout()
        buttons.addWidget(start_btn); buttons.addWidget(stop_btn); buttons.addStretch(1)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addLayout(buttons)
        root.addWidget(self.status)
        root.addWidget(note)
        root.addStretch(1)

    def set_sources(self, sources: list[str], preferred: str | None = None) -> None:
        self.source.clear()
        self.source.addItems(sources)
        if preferred:
            idx = self.source.findText(preferred)
            if idx >= 0:
                self.source.setCurrentIndex(idx)
        else:
            for i, name in enumerate(sources):
                if name.endswith(".monitor"):
                    self.source.setCurrentIndex(i)
                    break


class LibraryTab(QWidget):
    """Browse the scanned local library and send tracks to a speaker.

    Artists narrow to albums, albums narrow to tracks, and search cuts straight
    to tracks. Playing anything from here needs the media server, which the
    controller starts on demand, because the speaker fetches the file itself.
    """

    play_tracks = pyqtSignal(list, int)   # the list shown, where to start
    enqueue = pyqtSignal(object)
    rescan = pyqtSignal()
    add_folder = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self._albums: list = []
        self._tracks: list[Tags] = []

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search title, artist or album…")
        self.search.textChanged.connect(self._search_changed)

        self.artists = QListWidget()
        self.artists.currentTextChanged.connect(self._artist_changed)
        self.albums = QListWidget()
        self.albums.currentRowChanged.connect(self._album_changed)

        self.tracks = QTableWidget(0, 4)
        frame_table(self.tracks)
        self.tracks.setHorizontalHeaderLabels(["#", "Title", "Artist", "Length"])
        self.tracks.verticalHeader().setVisible(False)
        self.tracks.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tracks.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        head = self.tracks.horizontalHeader()
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tracks.doubleClicked.connect(self._play_selected)

        play_btn = QPushButton("Play")
        play_btn.clicked.connect(self._play_selected)
        queue_btn = QPushButton("Add to queue")
        queue_btn.clicked.connect(self._enqueue_selected)
        scan_btn = QPushButton("Rescan")
        scan_btn.clicked.connect(self.rescan.emit)
        folder_btn = QPushButton("Add folder…")
        folder_btn.clicked.connect(self._pick_folder)

        self.stats = QLabel("No library scanned yet.")
        self.stats.setObjectName("hint")

        lists = QSplitter(Qt.Orientation.Horizontal)
        for title, widget in (("Artists", self.artists), ("Albums", self.albums)):
            box = QWidget(); lay = QVBoxLayout(box)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(QLabel(title)); lay.addWidget(widget)
            lists.addWidget(box)
        tracks_box = QWidget(); tlay = QVBoxLayout(tracks_box)
        tlay.setContentsMargins(0, 0, 0, 0)
        tlay.addWidget(QLabel("Tracks")); tlay.addWidget(self.tracks)
        lists.addWidget(tracks_box)
        lists.setSizes([180, 220, 460])

        buttons = QHBoxLayout()
        buttons.addWidget(play_btn); buttons.addWidget(queue_btn)
        buttons.addStretch(1)
        buttons.addWidget(folder_btn); buttons.addWidget(scan_btn)

        root = QVBoxLayout(self)
        root.addWidget(self.search)
        root.addWidget(lists, 1)
        root.addLayout(buttons)
        root.addWidget(self.stats)

    def _pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add a music folder")
        if folder:
            self.add_folder.emit(folder)

    def load(self, library) -> None:
        self._library = library
        self.artists.clear()
        self.artists.addItem("All artists")
        self.artists.addItems(library.artists())
        self.artists.setCurrentRow(0)
        s = library.stats()
        self.stats.setText(
            f"{s['tracks']} tracks · {s['albums']} albums · {s['artists']} artists · "
            f"{fmt_time(s['duration'])}"
        )

    def _artist_changed(self, name: str) -> None:
        if not hasattr(self, "_library"):
            return
        self._albums = (
            self._library.albums() if name in ("", "All artists")
            else self._library.by_artist(name)
        )
        self.albums.clear()
        for album in self._albums:
            label = f"{album.name} ({album.year})" if album.year else album.name
            self.albums.addItem(label)
        if self._albums:
            self.albums.setCurrentRow(0)
        else:
            self._show_tracks([])

    def _album_changed(self, row: int) -> None:
        if 0 <= row < len(self._albums):
            self._show_tracks(self._albums[row].sorted_tracks())

    def _search_changed(self, text: str) -> None:
        if text.strip() and hasattr(self, "_library"):
            self._show_tracks(self._library.search(text))
        elif hasattr(self, "_library"):
            self._album_changed(self.albums.currentRow())

    def _show_tracks(self, tracks: list[Tags]) -> None:
        self._tracks = tracks
        self.tracks.setRowCount(len(tracks))
        for row, t in enumerate(tracks):
            cells = (
                str(t.track or ""), t.display_title,
                t.artist or t.display_artist, fmt_time(t.duration),
            )
            for col, value in enumerate(cells):
                self.tracks.setItem(row, col, QTableWidgetItem(value))
        self.tracks.resizeColumnToContents(0)
        self.tracks.resizeColumnToContents(3)

    def _selected(self) -> list[Tags]:
        rows = sorted({i.row() for i in self.tracks.selectedIndexes()})
        return [self._tracks[r] for r in rows if 0 <= r < len(self._tracks)]

    def _play_selected(self) -> None:
        rows = sorted({i.row() for i in self.tracks.selectedIndexes()})
        if rows and 0 <= rows[0] < len(self._tracks):
            # Play on down the album or search shown, not just the one file.
            self.play_tracks.emit(list(self._tracks), rows[0])

    def _enqueue_selected(self) -> None:
        picked = self._selected()
        if picked:
            self.enqueue.emit(picked)


class QueueTab(QWidget):
    """The speaker's own queue, each entry with its cover."""

    ART = 40
    #: Rows beyond the visible ones whose covers are fetched ahead of scrolling.
    ART_MARGIN = 6

    play_index = pyqtSignal(int)
    remove_index = pyqtSignal(int)
    clear_queue = pyqtSignal()
    move = pyqtSignal(int, int)      # 0-based row, -1 up / +1 down
    save = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["#", "Title", "Artist"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        frame_table(self.table)
        self.table.setIconSize(QSize(self.ART, self.ART))
        self.table.verticalHeader().setDefaultSectionSize(self.ART + 12)
        self.table.verticalScrollBar().valueChanged.connect(lambda _v: self._fetch_visible_art())
        self.table.doubleClicked.connect(
            lambda idx: self.play_index.emit(idx.row())
        )
        self._loader = None
        self._arts: list[str] = []

        play_btn = QPushButton("Play")
        play_btn.clicked.connect(self._play)
        rm_btn = QPushButton("Remove")
        rm_btn.clicked.connect(self._remove)
        clear_btn = QPushButton("Clear queue")
        clear_btn.clicked.connect(self.clear_queue.emit)
        up_btn = QPushButton("↑"); up_btn.setToolTip("Move up")
        up_btn.clicked.connect(lambda: self._move(-1))
        down_btn = QPushButton("↓"); down_btn.setToolTip("Move down")
        down_btn.clicked.connect(lambda: self._move(+1))
        save_btn = QPushButton("Save as playlist…")
        save_btn.clicked.connect(self.save.emit)

        buttons = QHBoxLayout()
        buttons.addWidget(play_btn); buttons.addWidget(rm_btn)
        buttons.addWidget(up_btn); buttons.addWidget(down_btn)
        buttons.addStretch(1); buttons.addWidget(save_btn); buttons.addWidget(clear_btn)

        root = QVBoxLayout(self)
        root.addWidget(self.table, 1)
        root.addLayout(buttons)

    def _current(self) -> int:
        return self.table.currentRow()

    def _play(self) -> None:
        if self._current() >= 0:
            self.play_index.emit(self._current())

    def _remove(self) -> None:
        if self._current() >= 0:
            self.remove_index.emit(self._current())

    def _move(self, step: int) -> None:
        row = self._current()
        if row < 0 or not (0 <= row + step < self.table.rowCount()):
            return
        self.move.emit(row, step)
        # Follow the entry so repeated presses keep moving the same one.
        self.table.selectRow(row + step)

    def set_art_loader(self, loader) -> None:
        """Share the browser's artwork loader and its cache."""
        self._loader = loader
        loader.ready.connect(self._art_landed)

    def _placeholder(self) -> QIcon:
        # One shared tile: a queue can hold a thousand entries.
        return QIcon(self._loader.placeholder(SimpleNamespace(title="", kind="track"), self.ART))

    def _fetch_visible_art(self) -> None:
        """Covers for the rows on screen, and a few either side.

        Every queue entry has its own cover address, even within one album, so
        asking for all of them would send up to a thousand requests through the
        speaker at once. Scrolling asks for the next ones as they come into view.
        """
        if self._loader is None or not self._arts:
            return
        first = self.table.rowAt(0)
        last = self.table.rowAt(self.table.viewport().height() - 1)
        first = 0 if first < 0 else first
        last = len(self._arts) - 1 if last < 0 else last
        ahead = [*range(max(0, first - self.ART_MARGIN), first),
                 *range(last + 1, min(len(self._arts), last + self.ART_MARGIN + 1))]
        # The loader serves the latest request first, so the rows either side
        # are asked for before the visible ones, and those from the bottom up:
        # the top of the screen fills in first.
        for row in [*ahead, *range(last, first - 1, -1)]:
            self._show_art(row)

    def _show_art(self, row: int) -> None:
        item = self.table.item(row, 1)
        url = self._arts[row] if row < len(self._arts) else ""
        if item is None:
            return
        pix = self._loader.pixmap(url, self.ART) if url else None
        item.setIcon(QIcon(pix) if pix is not None else self._placeholder())

    def _art_landed(self, url: str) -> None:
        for row, art in enumerate(self._arts):
            if art == url:
                self._show_art(row)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fetch_visible_art()

    def load(self, items: list, arts: list[str] | None = None) -> None:
        """Show `items`; `arts` holds each entry's full cover address, or ""."""
        self._arts = self._one_per_album(items, arts) if arts is not None \
            else [""] * len(items)
        self.table.setRowCount(len(items))
        self._uris = []
        for row, item in enumerate(items):
            title = getattr(item, "title", "") or ""
            artist = getattr(item, "creator", "") or ""
            for col, value in enumerate((str(row + 1), title, artist)):
                self.table.setItem(row, col, QTableWidgetItem(value))
            resources = getattr(item, "resources", None) or []
            self._uris.append(resources[0].uri if resources else "")
        # Wide enough for the playing row's number in bold, so the column does
        # not jump when the mark moves.
        bold = QFont(self.table.font()); bold.setBold(True)
        widest = str(max(len(items), 1))
        self.table.setColumnWidth(0, QFontMetrics(bold).horizontalAdvance(widest) + 28)
        self._playing_row = -1  # every row was just rebuilt unstyled
        self._show_playing()
        if self._loader is not None:
            placeholder = self._placeholder()
            for row in range(len(items)):
                self.table.item(row, 1).setIcon(placeholder)
            QTimer.singleShot(0, self._fetch_visible_art)  # once rows are laid out

    @staticmethod
    def _one_per_album(items: list, arts: list[str]) -> list[str]:
        """Give songs from one album the same cover address.

        The speaker names each song's cover separately even when they are all
        the album's, and fetching one is slow, so a whole album costs one fetch.
        Albums are told apart by artist too: many are called "Greatest Hits".
        """
        first: dict[tuple[str, str], str] = {}
        out = list(arts)
        for row, (item, art) in enumerate(zip(items, arts)):
            album = getattr(item, "album", "") or ""
            if album and art:
                out[row] = first.setdefault((album, getattr(item, "creator", "") or ""), art)
        return out

    def set_playing(self, position: int, uri: str, state: str) -> None:
        """Mark the queue entry the speaker is on.

        The speaker reports a queue position even when it is playing something
        that is not the queue (radio, the desktop stream), and the queue can
        change under it. So a row is only marked when the entry at that position
        is also the song the speaker names.
        """
        self._now = (position, uri, state)
        self._show_playing()

    @staticmethod
    def _same_song(a: str, b: str) -> bool:
        # The query string carries per-account flags that may be written
        # differently in the queue and in the transport; the body identifies
        # the song.
        return bool(a) and a.split("?", 1)[0] == b.split("?", 1)[0]

    def _show_playing(self) -> None:
        position, uri, _state = getattr(self, "_now", (0, "", ""))
        uris = getattr(self, "_uris", [])
        row = position - 1
        if not (0 <= row < len(uris) and self._same_song(uris[row], uri)):
            row = -1
        previous = getattr(self, "_playing_row", -1)
        if previous != row and 0 <= previous < self.table.rowCount():
            self._style_row(previous, False)
        if row >= 0:
            self._style_row(row, True)
            if row != previous:
                self.table.scrollToItem(self.table.item(row, 1),
                                        QAbstractItemView.ScrollHint.EnsureVisible)
        self._playing_row = row

    def _style_row(self, row: int, on: bool) -> None:
        # Colour and weight alone mark the song; the play/pause button already
        # says whether it is playing.
        for col in range(self.table.columnCount()):
            item = self.table.item(row, col)
            if item is None:
                continue
            font = item.font(); font.setBold(on); item.setFont(font)
            if on:
                item.setForeground(QColor(style.C["accent"]))
            else:
                item.setData(Qt.ItemDataRole.ForegroundRole, None)

    def restyle(self) -> None:
        """Recolour the playing row after a theme change."""
        self._playing_row = -1
        self._show_playing()

    @property
    def playing_row(self) -> int:
        return getattr(self, "_playing_row", -1)




class FavouritesTab(QWidget):
    """Sonos favourites and saved playlists, shared by every controller.

    These are the same lists the official app shows, stored on the speakers
    rather than on this machine, so anything saved from a phone appears here.
    """

    play_favourite = pyqtSignal(object)
    play_playlist = pyqtSignal(object)
    reload = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._favs: list = []
        self._lists: list = []

        self.favs = QListWidget()
        self.favs.itemDoubleClicked.connect(lambda _i: self._play_fav())
        self.lists = QListWidget()
        self.lists.itemDoubleClicked.connect(lambda _i: self._play_list())

        fav_btn = QPushButton("Play favourite")
        fav_btn.clicked.connect(self._play_fav)
        list_btn = QPushButton("Play playlist")
        list_btn.clicked.connect(self._play_list)
        reload_btn = QPushButton("Reload")
        reload_btn.clicked.connect(self.reload.emit)

        split = QSplitter(Qt.Orientation.Horizontal)
        for title, widget, btn in (("Sonos favourites", self.favs, fav_btn),
                                   ("Sonos playlists", self.lists, list_btn)):
            box = QWidget(); lay = QVBoxLayout(box)
            lay.setContentsMargins(0, 0, 0, 0)
            head = QLabel(title); head.setObjectName("sectionTitle")
            lay.addWidget(head); lay.addWidget(widget, 1); lay.addWidget(btn)
            split.addWidget(box)

        note = QLabel(
            "Streams play at once. Albums and playlists replace the queue and play "
            "from the top."
        )
        note.setObjectName("hint"); note.setWordWrap(True)

        bottom = QHBoxLayout()
        bottom.addWidget(note, 1); bottom.addWidget(reload_btn)

        root = QVBoxLayout(self)
        root.addWidget(split, 1)
        root.addLayout(bottom)

    def load(self, favs: list, lists: list) -> None:
        self._favs, self._lists = favs, lists
        self.favs.clear(); self.lists.clear()
        for f in favs:
            self.favs.addItem(f.title)
        for pl in lists:
            self.lists.addItem(pl.title)
        if not favs:
            self.favs.addItem(QListWidgetItem("(no favourites saved)"))
        if not lists:
            self.lists.addItem(QListWidgetItem("(no playlists saved)"))

    def _play_fav(self) -> None:
        row = self.favs.currentRow()
        if 0 <= row < len(self._favs):
            self.play_favourite.emit(self._favs[row])

    def _play_list(self) -> None:
        row = self.lists.currentRow()
        if 0 <= row < len(self._lists):
            self.play_playlist.emit(self._lists[row])


class AlarmDialog(QDialog):
    """Pick a time, a repeat pattern and a volume for a new alarm."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New alarm")
        self.time = QTimeEdit(QTime(7, 0)); self.time.setDisplayFormat("HH:mm")
        self.repeat = QComboBox()
        for key, label in RECURRENCES:
            self.repeat.addItem(label, key)
        self.repeat.addItem("Chosen days…", "CUSTOM")
        self.days = [QCheckBox(d) for d in DAY_NAMES]
        days_row = QWidget(); dl = QHBoxLayout(days_row); dl.setContentsMargins(0, 0, 0, 0)
        for box in self.days:
            dl.addWidget(box)
        days_row.setVisible(False)
        self.repeat.currentIndexChanged.connect(
            lambda _i: days_row.setVisible(self.repeat.currentData() == "CUSTOM")
        )
        self.volume = QSpinBox(); self.volume.setRange(0, 100); self.volume.setValue(20)
        self.length = QSpinBox(); self.length.setRange(0, 600); self.length.setValue(60)
        self.length.setSuffix(" min"); self.length.setSpecialValueText("until stopped")

        form = QFormLayout(self)
        form.addRow("Time", self.time)
        form.addRow("Repeat", self.repeat)
        form.addRow("", days_row)
        form.addRow("Volume", self.volume)
        form.addRow("Plays for", self.length)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> dict:
        t = self.time.time()
        code = self.repeat.currentData()
        if code == "CUSTOM":
            picked = "".join(str(i) for i, b in enumerate(self.days) if b.isChecked())
            code = f"ON_{picked}" if picked else "ONCE"
        minutes = self.length.value()
        return {
            "start": datetime.time(t.hour(), t.minute()),
            "recurrence": code,
            "volume": self.volume.value(),
            "duration": datetime.time(minutes // 60, minutes % 60) if minutes else None,
        }


class AlarmsTab(QWidget):
    """The household's alarms: every room, not just the selected speaker."""

    create = pyqtSignal(dict)
    toggle = pyqtSignal(object, bool)
    delete = pyqtSignal(object)
    reload = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._alarms: list = []
        self.table = QTableWidget(0, 5)
        frame_table(self.table)
        self.table.setHorizontalHeaderLabels(["On", "Time", "Repeat", "Room", "Volume"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._item_changed)

        new_btn = QPushButton("New alarm…")
        new_btn.clicked.connect(self._new)
        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(self._delete)
        reload_btn = QPushButton("Reload")
        reload_btn.clicked.connect(self.reload.emit)

        self.next_label = QLabel("")
        self.next_label.setObjectName("hint")

        buttons = QHBoxLayout()
        buttons.addWidget(new_btn); buttons.addWidget(del_btn)
        buttons.addStretch(1); buttons.addWidget(reload_btn)

        root = QVBoxLayout(self)
        root.addWidget(self.table, 1)
        root.addWidget(self.next_label)
        root.addLayout(buttons)

    def load(self, alarms: list) -> None:
        self._alarms = alarms
        self.table.blockSignals(True)
        self.table.setRowCount(len(alarms))
        soonest = None
        for row, a in enumerate(alarms):
            on = QTableWidgetItem()
            on.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            on.setCheckState(Qt.CheckState.Checked if a.enabled else Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, on)
            try:
                room = a.zone.player_name
            except Exception:
                room = "?"
            cells = (a.start_time.strftime("%H:%M"), describe_recurrence(a.recurrence),
                     room, str(a.volume))
            for col, text in enumerate(cells, 1):
                self.table.setItem(row, col, QTableWidgetItem(text))
            if a.enabled:
                try:
                    when = a.get_next_alarm_datetime()
                except Exception:
                    when = None
                if when and (soonest is None or when < soonest[0]):
                    soonest = (when, room)
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(1)
        self.table.blockSignals(False)
        if soonest:
            self.next_label.setText(
                f"Next: {soonest[0].strftime('%a %d %b, %H:%M')} in {soonest[1]}"
            )
        else:
            self.next_label.setText("No alarm is armed." if alarms else "No alarms set.")

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0 and 0 <= item.row() < len(self._alarms):
            self.toggle.emit(self._alarms[item.row()],
                             item.checkState() == Qt.CheckState.Checked)

    def _new(self) -> None:
        dlg = AlarmDialog(self)
        if dlg.exec():
            self.create.emit(dlg.values())

    def _delete(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self._alarms):
            self.delete.emit(self._alarms[row])


class DevicePanel(QWidget):
    """The unit itself: its name, lights, buttons, battery, sleep timer."""

    rename = pyqtSignal(str)
    line_in = pyqtSignal()
    tv = pyqtSignal()
    set_led = pyqtSignal(bool)
    set_buttons = pyqtSignal(bool)
    sleep = pyqtSignal(object)
    forget = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._loading = False
        self.name = QLineEdit()
        rename_btn = QPushButton("Rename")
        rename_btn.clicked.connect(lambda: self.rename.emit(self.name.text().strip()))
        name_row = QHBoxLayout(); name_row.addWidget(self.name, 1); name_row.addWidget(rename_btn)

        self.led = QCheckBox("Status light on")
        self.led.toggled.connect(lambda v: None if self._loading else self.set_led.emit(v))
        self.buttons = QCheckBox("Touch controls enabled")
        self.buttons.toggled.connect(
            lambda v: None if self._loading else self.set_buttons.emit(v)
        )

        self.line_in_btn = QPushButton("Play from line-in")
        self.line_in_btn.clicked.connect(self.line_in.emit)
        self.tv_btn = QPushButton("Play from TV")
        self.tv_btn.clicked.connect(self.tv.emit)
        sources = QHBoxLayout()
        sources.addWidget(self.line_in_btn); sources.addWidget(self.tv_btn); sources.addStretch(1)

        self.battery = QLabel("—")
        self.sleep_left = QLabel("off")
        self.sleep_minutes = QSpinBox(); self.sleep_minutes.setRange(1, 1440)
        self.sleep_minutes.setValue(30); self.sleep_minutes.setSuffix(" min")
        set_sleep = QPushButton("Start")
        set_sleep.clicked.connect(lambda: self.sleep.emit(self.sleep_minutes.value() * 60))
        cancel_sleep = QPushButton("Cancel")
        cancel_sleep.clicked.connect(lambda: self.sleep.emit(None))
        sleep_row = QHBoxLayout()
        sleep_row.addWidget(self.sleep_minutes); sleep_row.addWidget(set_sleep)
        sleep_row.addWidget(cancel_sleep); sleep_row.addWidget(self.sleep_left, 1)

        self.details = QTextEdit(); self.details.setReadOnly(True)
        self.details.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        forget_btn = QPushButton("Forget this speaker")
        forget_btn.setObjectName("danger")
        forget_btn.clicked.connect(self.forget.emit)

        form = QFormLayout()
        form.addRow("Room name", name_row)
        form.addRow("", self.led)
        form.addRow("", self.buttons)
        form.addRow("Inputs", sources)
        form.addRow("Battery", self.battery)
        form.addRow("Sleep timer", sleep_row)

        bottom = QHBoxLayout(); bottom.addStretch(1); bottom.addWidget(forget_btn)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(QLabel("Details"))
        root.addWidget(self.details, 1)
        root.addLayout(bottom)

    def load(self, status: dict, caps: list[str] | None = None) -> None:
        self._loading = True
        self.name.setText(status.get("name", ""))
        led = status.get("status_light")
        self.led.setVisible(led is not None)
        self.led.setChecked(bool(led))
        btns = status.get("buttons_enabled")
        self.buttons.setVisible(btns is not None)
        self.buttons.setChecked(bool(btns))
        caps = caps or []
        self.line_in_btn.setVisible("LINE_IN" in caps)
        self.tv_btn.setVisible(bool(status.get("is_soundbar")) or "HT_PLAYBACK" in caps)
        bat = status.get("battery")
        if bat:
            source = str(bat.get("PowerSource", "")).replace("_", " ").lower()
            self.battery.setText(
                f"{bat.get('Level', '?')}%  ·  health {str(bat.get('Health', '?')).lower()}"
                f"  ·  {bat.get('Temperature', '?').lower()} temperature  ·  {source}"
            )
        else:
            self.battery.setText("mains powered")
        lines = [f"{k.replace('_', ' '):<26} {v}" for k, v in status.items()
                 if v is not None and k != "battery"]
        if caps:
            lines.append(f"{'capabilities':<26} {', '.join(caps)}")
        self.details.setPlainText("\n".join(lines))
        self._loading = False

    def set_sleep_remaining(self, seconds: int | None) -> None:
        if not seconds:
            self.sleep_left.setText("off")
        else:
            h, rem = divmod(int(seconds), 3600)
            m, sec = divmod(rem, 60)
            self.sleep_left.setText(f"{h}:{m:02d}:{sec:02d} left" if h else f"{m}:{sec:02d} left")


class LinkDialog(QDialog):
    """Hand the user a service's login address and wait for them to finish."""

    def __init__(self, service: str, url: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Link {service}")
        text = QLabel(
            f"Open this address, log in to {service} there and approve Sonolin. "
            "Your password goes to the service, never to this app. "
            "Then come back and press Done."
        )
        text.setWordWrap(True)
        self.url = QLineEdit(url); self.url.setReadOnly(True)
        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(url))
        open_btn = QPushButton("Open in browser")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
        row = QHBoxLayout(); row.addWidget(self.url, 1); row.addWidget(copy_btn)
        row.addWidget(open_btn)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Done")
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        root = QVBoxLayout(self)
        root.addWidget(text); root.addLayout(row); root.addWidget(buttons)
        self.resize(560, 160)
