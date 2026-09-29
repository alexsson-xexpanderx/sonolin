"""The music-service browser: artwork, pages and a right-click menu.

Search results come back as sections, like a music app: songs as rows, artists
as round portraits, albums and playlists as covers. Opening anything gives it a
page with a large header. Artists get their popular songs, radio and
discography; albums and playlists get a numbered track list. The track playing
now is highlighted wherever it appears.

Unlike the other panels, the browser runs its own background jobs. Navigation
is request and response by nature, and routing every page load through the
window would only add plumbing. It still never touches a speaker directly: every
call goes through `Services` on the thread pool via `workers.run`, and only
data comes back to the GUI thread.

Artwork is fetched by Qt's own network stack straight from the services' image
servers, asynchronously on the GUI thread, with a disk cache under
``$XDG_CACHE_HOME/sonolin/art`` so a page seen once opens instantly afterwards.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import math
import os
import socket
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from urllib.parse import unquote

from PyQt6.QtCore import (
    QAbstractListModel, QEvent, QModelIndex, QObject, QPointF, QRect, QRectF, QSize,
    Qt, QThreadPool, QTimer, QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QColor, QFont, QFontMetrics, QGuiApplication, QImage, QLinearGradient, QPainter,
    QPainterPath, QPixmap, QPolygonF,
)
from PyQt6.QtNetwork import (
    QNetworkAccessManager, QNetworkCacheMetaData, QNetworkDiskCache, QNetworkProxy,
    QNetworkReply, QNetworkRequest,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QComboBox, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListView, QMenu, QPushButton, QScrollArea, QSizePolicy,
    QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from ..services import Entry, Services, describe, is_personal
from . import style, workers

log = logging.getLogger(__name__)

ENTRY_ROLE = Qt.ItemDataRole.UserRole + 10

CARD_W, CARD_H, CARD_ART = 184, 264, 156
ROW_ART_H, ROW_PLAIN_H = 58, 46
HERO_ART = 188

#: How a search for everything is laid out, and how much of each is shown.
SEARCH_SECTIONS = (
    ("track", "Songs", "tracks", 5),
    ("artist", "Artists", "artists", 7),
    ("album", "Albums", "albums", 7),
    ("playlist", "Playlists", "playlists", 7),
    ("program", "Radio", None, 7),
    ("stream", "Stations", None, 14),
    ("folder", "More", None, 14),
)

KIND_LABELS = {
    "artist": "ARTIST", "album": "ALBUM", "playlist": "PLAYLIST",
    "program": "RADIO", "stream": "RADIO STATION", "folder": "BROWSE",
    "track": "SONG",
}


def cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    path = base / "sonolin" / "art"
    path.mkdir(parents=True, exist_ok=True)
    return path


def fmt_duration(seconds: float, long: bool = False) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if long:
        return f"{h} h {m} min" if h else f"{m} min"
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def track_id_from_uri(uri: str) -> str:
    """``x-sonos-spotify:spotify%3atrack%3aABC?sid=9`` -> ``spotify:track:ABC``.

    A speaker reports what it plays as a URI; a service lists items by id. The
    id is the URI's body, percent-encoded, so decoding it is enough to match
    the two and highlight the song that is playing.
    """
    if not uri or ":" not in uri:
        return ""
    body = uri.split(":", 1)[1].split("?", 1)[0]
    return unquote(body)


# -- artwork ---------------------------------------------------------------


def _dpr() -> float:
    screen = QGuiApplication.primaryScreen()
    return screen.devicePixelRatio() if screen else 1.0


#: Tile colours. A fixed set of clearly different hues: hashing a name onto a
#: continuous hue wheel lets neighbouring tiles land on near-identical colours.
TILE_COLOURS = (
    "#e0533d", "#8d67ab", "#2d5aa0", "#e8115b", "#1e9e5a", "#b06a2c",
    "#dc148c", "#477d95", "#6a4fd8", "#0d8ec9", "#9a8c13", "#27856a",
)


def _gradient(title: str, px: int) -> QLinearGradient:
    """A two-tone gradient chosen by `title`, stable across runs."""
    digest = hashlib.md5(title.encode("utf-8")).digest()
    base = QColor(TILE_COLOURS[int.from_bytes(digest[:4], "big") % len(TILE_COLOURS)])
    grad = QLinearGradient(0, 0, px, px)
    grad.setColorAt(0, base.lighter(112))
    grad.setColorAt(1, base.darker(210))
    return grad


def _is_icon(image: QImage) -> bool:
    """True for monochrome glyph art: pale shapes on a dark, colourless field.

    Services illustrate their menus with such icons. Photos and covers have
    colour or light backgrounds and fail one of the two tests.
    """
    sample = image.scaled(12, 12, Qt.AspectRatioMode.IgnoreAspectRatio,
                          Qt.TransformationMode.FastTransformation)
    sat = val = 0.0
    for y in range(12):
        for x in range(12):
            c = sample.pixelColor(x, y)
            sat += c.hsvSaturationF()
            val += c.valueF()
    return sat / 144 < 0.08 and val / 144 < 0.45


def _shape(image: QImage, size: int, round_: bool, radius: float = 10,
           tint: str | None = None) -> QPixmap:
    """Cover-crop `image` to a square and clip it to a rounded or round shape.

    With `tint`, icon-like art is laid over a gradient in screen mode, so the
    black field takes the gradient's colour and the white glyph stays white.
    """
    dpr = _dpr()
    px = max(1, int(size * dpr))
    scaled = image.scaled(px, px, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                          Qt.TransformationMode.SmoothTransformation)
    scaled = scaled.copy((scaled.width() - px) // 2, (scaled.height() - px) // 2, px, px)
    out = QPixmap(px, px)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    if round_:
        path.addEllipse(0, 0, px, px)
    else:
        path.addRoundedRect(0, 0, px, px, radius * dpr, radius * dpr)
    painter.setClipPath(path)
    if tint is not None and _is_icon(image):
        painter.fillRect(0, 0, px, px, _gradient(tint, px))
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Screen)
    painter.drawImage(0, 0, scaled)
    painter.end()
    out.setDevicePixelRatio(dpr)
    return out


#: Where a speaker serves covers (``/getaa``), as for all its UPnP traffic.
SPEAKER_PORT = 1400


def _public_art_address(host: str, port: int) -> str:
    """Resolve once; the request must connect to this IP, never resolve again."""
    addresses = [ipaddress.ip_address(row[4][0]) for row in
                 socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
    if not addresses or any(
        not ip.is_global or ip.is_multicast or ip.is_reserved
        or (ip.version == 6 and (ip not in ipaddress.ip_network("2000::/3")
                                or ip.sixtofour is not None or ip.teredo is not None))
        for ip in addresses
    ):
        raise ValueError("non-public artwork address")
    return str(addresses[0])


class _Lane:
    """Work done a few at a time, the latest request first.

    What is on screen was asked for last, so it goes ahead of whatever was asked
    for while scrolling past it. Asking again for something still waiting moves
    it to the front. `later` puts off starting until a whole screenful has been
    asked for; otherwise the first few asked would always start first.
    """

    def __init__(self, slots: int, start: Callable[[str], None],
                 later: Callable[[Callable[[], None]], None] = lambda fn: fn()) -> None:
        self.slots, self.start, self.later = slots, start, later
        self.waiting: dict[str, None] = {}  # insertion-ordered; the newest is last
        self.running: set[str] = set()
        self._due = False

    def ask(self, key: str) -> None:
        if key in self.running:
            return
        self.waiting.pop(key, None)
        self.waiting[key] = None
        if not self._due:
            self._due = True
            self.later(self._pump)

    def done(self, key: str) -> None:
        self.running.discard(key)
        self._pump()

    def _pump(self) -> None:
        self._due = False
        while self.waiting and len(self.running) < self.slots:
            key, _ = self.waiting.popitem()
            self.running.add(key)
            self.start(key)


class ArtLoader(QObject):
    """Fetches, caches and shapes artwork. `ready(url)` fires when one lands.

    Covers served by a speaker are slow: it makes them one at a time, about a
    quarter of a second each, so a screenful of queue takes seconds. Two things
    help. A `resolver` can name a faster address for the same picture, such as
    the service's own image server, which serves many at once. And what still
    has to come from a speaker is asked for two at a time, newest first, so the
    covers on screen never wait behind ones scrolled past.
    """

    ready = pyqtSignal(str)
    MAX_IMAGES = 400
    #: Covers asked of one speaker at once; it answers them in turn anyway.
    SPEAKER_SLOTS = 2
    #: Resolver lookups in flight at once.
    LOOKUP_SLOTS = 6
    #: Resolved addresses remembered between runs, so a cover seen once loads
    #: straight from the disk cache next time, without a lookup.
    MAX_SOURCES = 5000

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.nam = QNetworkAccessManager(self)
        # A proxy could resolve a hostname again or route to a private network.
        self.nam.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        disk = QNetworkDiskCache(self)
        disk.setCacheDirectory(str(cache_dir()))
        disk.setMaximumCacheSize(250 * 1024 * 1024)
        self.nam.setCache(disk)
        self._images: OrderedDict[str, QImage] = OrderedDict()
        self._pixmaps: dict[tuple, QPixmap] = {}
        self._placeholders: dict[tuple, QPixmap] = {}
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        #: Given an artwork address, returns a blocking function that finds a
        #: faster address for the same picture ("" for none), or None when it
        #: has nothing to offer. The function runs off the GUI thread.
        # Only application-owned local artwork endpoints may bypass public DNS.
        # The callback runs on the GUI thread and must use trusted app state.
        self.local_artwork: Callable[[QUrl], bool] = lambda url: False
        self.resolver: Callable[[str], Callable[[], str] | None] | None = None
        self._aliases: dict[str, str] = {}      # address asked for -> address fetched
        self._askers: dict[str, set[str]] = {}  # address fetched -> addresses asked for
        self._lookups: dict[str, Callable[[], str]] = {}
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(self.LOOKUP_SLOTS)
        next_tick = partial(QTimer.singleShot, 0)
        self._lookup_lane = _Lane(self.LOOKUP_SLOTS, self._look_up, next_tick)
        self._speaker_lane = _Lane(self.SPEAKER_SLOTS, self._send, next_tick)
        self._sources_file = cache_dir().parent / "art-sources.json"
        self._save_soon = QTimer(self, singleShot=True, interval=2000)
        self._save_soon.timeout.connect(self._save_sources)
        for url, source in self._load_sources().items():
            self._aliases[url] = source
            self._askers.setdefault(source, set()).add(url)

    def _load_sources(self) -> dict[str, str]:
        try:
            data = json.loads(self._sources_file.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {u: s for u, s in data.items() if isinstance(u, str) and isinstance(s, str)}

    def _save_sources(self) -> None:
        known = [(u, s) for u, s in self._aliases.items() if u != s][-self.MAX_SOURCES:]
        draft = self._sources_file.with_suffix(".partial")
        try:
            draft.write_text(json.dumps(dict(known)))
            os.replace(draft, self._sources_file)
        except OSError as exc:
            log.debug("could not save artwork addresses: %s", exc)

    def pixmap(self, url: str, size: int, round_: bool = False,
               tint: str | None = None) -> QPixmap | None:
        """The artwork if it is here already; otherwise start fetching it."""
        if not url:
            return None
        source = self._aliases.get(url, url)
        if source in self._failed:
            return None
        key = (source, size, round_, tint)
        cached = self._pixmaps.get(key)
        if cached is not None:
            return cached
        image = self._images.get(source)
        if image is None:
            self._request(url)
            return None
        self._images.move_to_end(source)
        if len(self._pixmaps) > 1500:
            self._pixmaps.clear()
        shaped = self._pixmaps[key] = _shape(image, size, round_, tint=tint)
        return shaped

    def _request(self, url: str) -> None:
        if url in self._aliases:
            self._fetch(self._aliases[url])
        elif url in self._lookups:
            self._lookup_lane.ask(url)  # still wanted: move it up
        else:
            lookup = self.resolver(url) if self.resolver is not None else None
            if lookup is None or self._on_disk(url):
                self._use(url, url)
            else:
                self._lookups[url] = lookup
                self._lookup_lane.ask(url)

    def _on_disk(self, url: str) -> bool:
        cache = self.nam.cache()
        return cache is not None and cache.metaData(QUrl(url)).isValid()

    def _look_up(self, url: str) -> None:
        job = workers.Job(self._lookups[url])
        job.signals.done.connect(lambda found, u=url: self._looked_up(u, found))
        job.signals.failed.connect(lambda _e, u=url: self._looked_up(u, ""))
        self._pool.start(job)

    def _looked_up(self, url: str, found: str) -> None:
        self._lookups.pop(url, None)
        self._lookup_lane.done(url)
        self._use(url, found or url)

    def _use(self, url: str, source: str) -> None:
        """Fetch `url`'s picture from `source` from now on."""
        if self._aliases.get(url, url) != source:
            self._save_soon.start()
        self._aliases[url] = source
        self._askers.setdefault(source, set()).add(url)
        if source in self._images:
            self.ready.emit(url)
        else:
            self._fetch(source)

    def _fetch(self, url: str) -> None:
        if url in self._speaker_lane.waiting:
            self._speaker_lane.ask(url)  # still wanted: move it up
            return
        if url in self._pending or url in self._failed:
            return
        self._pending.add(url)
        if QUrl(url).port() == SPEAKER_PORT and not self._on_disk(url):
            self._speaker_lane.ask(url)
        else:
            self._send(url)

    def _send(self, url: str, target: QUrl | None = None, redirects: int = 0) -> None:
        target = QUrl(url) if target is None else target
        if (not target.isValid() or target.scheme() not in ("http", "https")
                or not target.host() or target.userInfo() or "%" in target.host()
                or target.port(80 if target.scheme() == "http" else 443) == 0):
            self._finish(url, b"")
            return
        if not redirects:
            cached = self.nam.cache().data(QUrl(url))
            if cached is not None:
                data = bytes(cached.readAll())
                cached.close()
                self._finish(url, data)
                return
        # Local exceptions require literal IPs: a trusted name must not rebind.
        if self.local_artwork(target):
            try:
                address = str(ipaddress.ip_address(target.host()))
            except ValueError:
                self._finish(url, b"")
            else:
                self._send_to(url, target, redirects, address)
            return
        host = target.host(QUrl.ComponentFormattingOption.FullyEncoded)
        port = target.port(80 if target.scheme() == "http" else 443)
        job = workers.Job(_public_art_address, host, port)
        job.signals.done.connect(lambda ip: self._send_to(url, target, redirects, ip))
        job.signals.failed.connect(lambda _e: self._finish(url, b""))
        self._pool.start(job)

    def _send_to(self, url: str, target: QUrl, redirects: int, address: str) -> None:
        pinned = QUrl(target)
        pinned.setHost(address)
        req = QNetworkRequest(pinned)
        host = target.host(QUrl.ComponentFormattingOption.FullyEncoded)
        authority = f"[{host}]" if ":" in host else host
        if target.port() != -1:
            authority += f":{target.port()}"
        req.setRawHeader(b"Host", authority.encode("ascii"))
        req.setPeerVerifyName(host)  # TLS certificate verification and SNI use the origin.
        # Qt builds HTTP/2 :authority from the IP URL, ignoring our Host header.
        req.setAttribute(QNetworkRequest.Attribute.Http2AllowedAttribute, False)
        req.setAttribute(QNetworkRequest.Attribute.CookieLoadControlAttribute,
                         QNetworkRequest.LoadControl.Manual)
        req.setAttribute(QNetworkRequest.Attribute.CookieSaveControlAttribute,
                         QNetworkRequest.LoadControl.Manual)
        req.setAttribute(QNetworkRequest.Attribute.AuthenticationReuseAttribute,
                         QNetworkRequest.LoadControl.Manual)
        req.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                         QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        # Cache by the original URL ourselves, not the pinned IP shared by CDNs.
        req.setAttribute(QNetworkRequest.Attribute.CacheLoadControlAttribute,
                         QNetworkRequest.CacheLoadControl.AlwaysNetwork)
        req.setAttribute(QNetworkRequest.Attribute.CacheSaveControlAttribute, False)
        req.setTransferTimeout(15000)
        reply = self.nam.get(req)
        reply.finished.connect(lambda: self._landed(url, reply, target, redirects))

    def _landed(self, url: str, reply: QNetworkReply, target: QUrl,
                redirects: int) -> None:
        redirect = reply.attribute(QNetworkRequest.Attribute.RedirectionTargetAttribute)
        ok = reply.error() == QNetworkReply.NetworkError.NoError
        data = bytes(reply.readAll()) if ok and redirect is None else b""
        reply.deleteLater()
        if ok and redirect is not None and redirects < 5:
            self._send(url, target.resolved(redirect), redirects + 1)
            return
        if data and not QImage.fromData(data).isNull():
            meta = QNetworkCacheMetaData()
            meta.setUrl(QUrl(url))
            meta.setRawHeaders([(b"Content-Length", str(len(data)).encode("ascii"))])
            device = self.nam.cache().prepare(meta)
            if device is not None:
                device.write(data)
                self.nam.cache().insert(device)
        self._finish(url, data)

    def _finish(self, url: str, data: bytes) -> None:
        self._pending.discard(url)
        self._speaker_lane.done(url)
        image = QImage()
        askers = self._askers.get(url, set()) - {url}
        if image.loadFromData(data):
            if image.width() > 640:  # service art is often 640 px or more
                image = image.scaled(640, 640, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
            self._images[url] = image
            while len(self._images) > self.MAX_IMAGES:
                old, _ = self._images.popitem(last=False)
                self._pixmaps = {k: v for k, v in self._pixmaps.items() if k[0] != old}
            self.ready.emit(url)
            for asker in askers:
                self.ready.emit(asker)
        else:
            self._failed.add(url)
            for asker in askers:  # the faster address let us down: go the slow way
                self._use(asker, asker)

    def placeholder(self, entry: Entry, size: int, round_: bool = False) -> QPixmap:
        """A gradient tile for items without artwork, coloured by name.

        The colour is derived from the title, so the same folder always gets the
        same tile, and neighbouring tiles rarely match.
        """
        key = (entry.title, entry.kind, size, round_)
        cached = self._placeholders.get(key)
        if cached is not None:
            return cached
        dpr = _dpr()
        px = max(1, int(size * dpr))
        pix = QPixmap(px, px)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        grad = _gradient(entry.title, px)
        path = QPainterPath()
        if round_:
            path.addEllipse(0, 0, px, px)
        else:
            path.addRoundedRect(0, 0, px, px, 10 * dpr, 10 * dpr)
        p.fillPath(path, grad)
        glyph = "♪" if entry.kind == "track" else "♫" if entry.kind == "playlist" \
            else (entry.title.strip()[:1] or "?").upper()
        font = QFont()
        font.setPixelSize(int(px * 0.42))
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor(255, 255, 255, 215))
        p.drawText(QRect(0, 0, px, px), Qt.AlignmentFlag.AlignCenter, glyph)
        p.end()
        pix.setDevicePixelRatio(dpr)
        self._placeholders[key] = pix
        return pix

    def art_for(self, entry: Entry, size: int, round_: bool = False) -> QPixmap:
        tint = entry.title if entry.kind == "folder" else None
        return self.pixmap(entry.art, size, round_, tint=tint) \
            or self.placeholder(entry, size, round_)


# -- model and painters ----------------------------------------------------


class EntryModel(QAbstractListModel):
    def __init__(self, entries: list[Entry], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.entries = list(entries)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.entries)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        e = self.entries[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return e.title
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{e.title}\n{e.subtitle}" if e.subtitle else e.title
        if role == ENTRY_ROLE:
            return e
        return None


def _play_glyph(p: QPainter, centre: QPointF, radius: float, colour: QColor) -> None:
    """A filled triangle, optically centred in a circle of `radius`."""
    r = radius * 0.42
    tri = QPolygonF([
        QPointF(centre.x() - r * 0.6, centre.y() - r),
        QPointF(centre.x() - r * 0.6, centre.y() + r),
        QPointF(centre.x() + r * 0.95, centre.y()),
    ])
    p.setBrush(colour)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawPolygon(tri)


def _two_lines(fm: QFontMetrics, text: str, width: int) -> list[str]:
    """Wrap `text` onto at most two lines, eliding only the second.

    One elided line hides exactly the words that tell similar names apart,
    such as "Discover Weekly" and "Discover Weekly October".
    """
    if fm.horizontalAdvance(text) <= width:
        return [text]
    words = text.split()
    first = ""
    for i, word in enumerate(words):
        trial = f"{first} {word}".strip()
        if fm.horizontalAdvance(trial) > width:
            if not first:  # a single word wider than the card
                return [fm.elidedText(text, Qt.TextElideMode.ElideRight, width)]
            rest = " ".join(words[i:])
            return [first, fm.elidedText(rest, Qt.TextElideMode.ElideRight, width)]
        first = trial
    return [first]


class CardDelegate(QStyledItemDelegate):
    """Artwork on top, title and a quieter line under it; a play button on hover."""

    def __init__(self, loader: ArtLoader, on_play: Callable[[Entry], None], parent=None) -> None:
        super().__init__(parent)
        self.loader = loader
        self.on_play = on_play

    def sizeHint(self, option, index) -> QSize:
        return QSize(CARD_W, CARD_H)

    def _art_rect(self, rect: QRect) -> QRect:
        return QRect(rect.x() + (rect.width() - CARD_ART) // 2, rect.y() + 14, CARD_ART, CARD_ART)

    def _button_rect(self, rect: QRect) -> QRectF:
        art = self._art_rect(rect)
        return QRectF(art.right() - 50, art.bottom() - 50, 42, 42)

    def paint(self, p: QPainter, option, index) -> None:
        e: Entry = index.data(ENTRY_ROLE)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(4, 4, -4, -4)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(style.C["raised"]))
            p.drawRoundedRect(QRectF(rect), 12, 12)
        round_ = e.kind == "artist"
        art = self._art_rect(option.rect)
        p.drawPixmap(art, self.loader.art_for(e, CARD_ART, round_))

        if hovered and e.playable:
            btn = self._button_rect(option.rect)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 90))
            p.drawEllipse(btn.translated(0, 2))
            p.setBrush(QColor(style.C["accent"]))
            p.drawEllipse(btn)
            _play_glyph(p, btn.center(), btn.width() / 2, QColor(style.C["bg"]))

        text_x = art.x() - 2 if not round_ else rect.x() + 10
        width = (art.width() + 4) if not round_ else rect.width() - 20
        align = Qt.AlignmentFlag.AlignCenter if round_ else Qt.AlignmentFlag.AlignLeft
        title_font = QFont(option.font); title_font.setBold(True); title_font.setPointSizeF(10.2)
        p.setFont(title_font)
        p.setPen(QColor(style.C["text"]))
        line_h = p.fontMetrics().height()
        y = art.bottom() + 11
        for line in _two_lines(p.fontMetrics(), e.title, width):
            p.drawText(QRect(text_x, y, width, line_h), align | Qt.AlignmentFlag.AlignVCenter, line)
            y += line_h
        sub_font = QFont(option.font); sub_font.setPointSizeF(9.0)
        p.setFont(sub_font)
        p.setPen(QColor(style.C["muted"]))
        sub = p.fontMetrics().elidedText(e.subtitle, Qt.TextElideMode.ElideRight, width)
        p.drawText(QRect(text_x, y + 3, width, p.fontMetrics().height()),
                   align | Qt.AlignmentFlag.AlignVCenter, sub)
        p.restore()

    def editorEvent(self, event, model, option, index) -> bool:
        if event.type() == QEvent.Type.MouseButtonRelease \
                and event.button() == Qt.MouseButton.LeftButton:
            e: Entry = index.data(ENTRY_ROLE)
            if e.playable and self._button_rect(option.rect).contains(event.position()):
                self.on_play(e)
                return True
        return False


class TrackDelegate(QStyledItemDelegate):
    """One song per row: number or thumbnail, title, a quieter line, length."""

    def __init__(self, loader: ArtLoader, show_art: bool, now_playing: Callable[[], str],
                 parent=None) -> None:
        super().__init__(parent)
        self.loader = loader
        self.show_art = show_art
        self.now_playing = now_playing

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), ROW_ART_H if self.show_art else ROW_PLAIN_H)

    def paint(self, p: QPainter, option, index) -> None:
        e: Entry = index.data(ENTRY_ROLE)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(2, 1, -2, -1)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        playing = bool(e.id) and e.id == self.now_playing()
        if selected or hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(style.C["accent_lo"] if selected else style.C["raised"]))
            p.drawRoundedRect(QRectF(rect), 8, 8)

        accent = QColor(style.C["accent"])
        x = rect.x() + 10
        num_rect = QRect(x, rect.y(), 28, rect.height())
        if hovered or playing:
            _play_glyph(p, QPointF(num_rect.center()), 11,
                        accent if playing else QColor(style.C["text"]))
        else:
            f = QFont(option.font); f.setPointSizeF(9.5)
            p.setFont(f)
            p.setPen(QColor(style.C["faint"]))
            p.drawText(num_rect, Qt.AlignmentFlag.AlignCenter,
                       str(e.number or index.row() + 1))
        x += 38
        if self.show_art:
            size = 42
            art = QRect(x, rect.y() + (rect.height() - size) // 2, size, size)
            p.drawPixmap(art, self.loader.pixmap(e.art, size) or self.loader.placeholder(e, size))
            x += size + 14

        dur_w = 64
        text_w = rect.right() - dur_w - x - 8
        title_font = QFont(option.font); title_font.setPointSizeF(10.3)
        title_font.setBold(playing)
        sub_font = QFont(option.font); sub_font.setPointSizeF(9.0)
        title_h = QFontMetrics(title_font).height()
        sub_h = QFontMetrics(sub_font).height()
        two_lines = bool(e.subtitle)
        block = title_h + (4 + sub_h if two_lines else 0)
        top = rect.y() + (rect.height() - block) // 2

        p.setFont(title_font)
        p.setPen(accent if playing else QColor(style.C["text"]))
        title = p.fontMetrics().elidedText(e.title, Qt.TextElideMode.ElideRight, text_w)
        p.drawText(QRect(x, top, text_w, title_h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        if two_lines:
            p.setFont(sub_font)
            p.setPen(QColor(style.C["muted"]))
            sub = p.fontMetrics().elidedText(e.subtitle, Qt.TextElideMode.ElideRight, text_w)
            p.drawText(QRect(x, top + title_h + 4, text_w, sub_h),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sub)

        f = QFont(option.font); f.setPointSizeF(9.5)
        p.setFont(f)
        p.setPen(QColor(style.C["muted"]))
        p.drawText(QRect(rect.right() - dur_w - 10, rect.y(), dur_w, rect.height()),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   fmt_duration(e.duration) if e.duration else "")
        p.restore()


# -- views -----------------------------------------------------------------


class _FittedView(QListView):
    """A list view as tall as its contents, for stacking inside one scroll area.

    A page is several views one above the other in a single scroll area, the
    way music apps lay out an artist. Each view therefore sizes itself to its
    rows and never scrolls on its own.
    """

    #: (all entries in this view, the row double-clicked) — songs play in context
    activated_at = pyqtSignal(list, int)
    #: (the selected entries, where to show the menu, all entries in this view)
    menu_requested = pyqtSignal(list, object, list)

    def __init__(self, entries: list[Entry], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setModel(EntryModel(entries, self))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.doubleClicked.connect(lambda idx: self.activated_at.emit(self.entries, idx.row()))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setObjectName("browserView")

    @property
    def entries(self) -> list[Entry]:
        return self.model().entries

    def _menu(self, pos) -> None:
        index = self.indexAt(pos)
        if not index.isValid():
            return
        rows = {i.row() for i in self.selectedIndexes()}
        if index.row() not in rows:
            self.clearSelection()
            self.setCurrentIndex(index)
            rows = {index.row()}
        picked = [self.entries[r] for r in sorted(rows)]
        self.menu_requested.emit(picked, self.viewport().mapToGlobal(pos), self.entries)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.fit()

    def fit(self) -> None:  # pragma: no cover - overridden
        pass


class CardGrid(_FittedView):
    def __init__(self, entries: list[Entry], loader: ArtLoader,
                 on_play: Callable[[Entry], None], parent=None) -> None:
        super().__init__(entries, parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setGridSize(QSize(CARD_W, CARD_H))
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setItemDelegate(CardDelegate(loader, on_play, self))
        self.setStyleSheet("QListView::item:selected { background: transparent; }")

    def fit(self) -> None:
        cols = max(1, self.viewport().width() // CARD_W)
        rows = math.ceil(len(self.entries) / cols)
        self.setFixedHeight(max(1, rows) * CARD_H + 6)


class TrackList(_FittedView):
    def __init__(self, entries: list[Entry], loader: ArtLoader, show_art: bool,
                 now_playing: Callable[[], str], parent=None) -> None:
        super().__init__(entries, parent)
        self.row_h = ROW_ART_H if show_art else ROW_PLAIN_H
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setUniformItemSizes(True)
        self.setItemDelegate(TrackDelegate(loader, show_art, now_playing, self))
        self.setStyleSheet("QListView::item { border: none; }"
                           "QListView::item:selected { background: transparent; }")

    def fit(self) -> None:
        self.setFixedHeight(len(self.entries) * self.row_h + 4)


# -- page parts ------------------------------------------------------------


class Hero(QWidget):
    """The big header at the top of an artist, album or playlist page."""

    def __init__(self, entry: Entry, detail: str, loader: ArtLoader,
                 buttons: list[QPushButton]) -> None:
        super().__init__()
        self.setObjectName("hero")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.entry, self.loader = entry, loader
        self.round_ = entry.kind == "artist"
        self.art = QLabel()
        self.art.setFixedSize(HERO_ART, HERO_ART)
        self._paint_art()
        loader.ready.connect(self._art_landed)

        kind = QLabel(KIND_LABELS.get(entry.kind, entry.kind.upper()))
        kind.setObjectName("heroKind")
        title = QLabel(entry.title)
        title.setObjectName("heroTitle")
        title.setWordWrap(True)
        sub = QLabel(detail)
        sub.setObjectName("heroSub")
        sub.setWordWrap(True)

        row = QHBoxLayout()
        row.setSpacing(10)
        for b in buttons:
            row.addWidget(b)
        row.addStretch(1)

        text = QVBoxLayout()
        text.setSpacing(6)
        text.addStretch(1)
        text.addWidget(kind)
        text.addWidget(title)
        text.addWidget(sub)
        text.addSpacing(10)
        text.addLayout(row)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(26)
        lay.addWidget(self.art, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(text, 1)

    def _paint_art(self) -> None:
        self.art.setPixmap(self.loader.art_for(self.entry, HERO_ART, self.round_))

    def _art_landed(self, url: str) -> None:
        if url == self.entry.art:
            self._paint_art()


def _section(title: str, body: QWidget, show_all: Callable[[], None] | None = None) -> QWidget:
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 18, 0, 0)
    lay.setSpacing(8)
    if not title and show_all is None:
        lay.addWidget(body)
        return box
    head = QHBoxLayout()
    label = QLabel(title)
    label.setObjectName("sectionHeader")
    head.addWidget(label)
    head.addStretch(1)
    if show_all is not None:
        more = QPushButton("Show all")
        more.setObjectName("linkButton")
        more.setCursor(Qt.CursorShape.PointingHandCursor)
        more.clicked.connect(show_all)
        head.addWidget(more)
    lay.addLayout(head)
    lay.addWidget(body)
    return box


@dataclass
class Location:
    """One place in the browser's history."""

    kind: str                  # "root", "open" or "search"
    title: str
    entry: Entry | None = None
    ref: object = None         # what to open: an item, or a bare id
    term: str = ""
    category: str = ""


# -- the browser -----------------------------------------------------------


class Browser(QWidget):
    """Browse, search and play music services, with pages and artwork."""

    status = pyqtSignal(str)
    error = pyqtSignal(str)
    played = pyqtSignal()
    link_requested = pyqtSignal(str)
    playlists_changed = pyqtSignal()
    service_changed = pyqtSignal(str)

    def __init__(self, services: Services, speaker: Callable[[], object]) -> None:
        super().__init__()
        self.services = services
        self.speaker = speaker
        self.loader = ArtLoader(self)
        self._history: list[Location] = []
        self._serial = 0
        self._now_id = ""
        self._views: list[_FittedView] = []
        self._playlists: list = []
        self._categories: list[str] = []
        self._catalogue: list = []
        self.loader.ready.connect(self._art_landed)

        self.back_btn = QPushButton("←")
        self.back_btn.setObjectName("navButton")
        self.back_btn.setToolTip("Back")
        self.back_btn.clicked.connect(self.back)
        self.home_btn = QPushButton("⌂")
        self.home_btn.setObjectName("navButton")
        self.home_btn.setToolTip("Top of the service")
        self.home_btn.clicked.connect(self.home)

        self.service = QComboBox()
        self.service.setMinimumWidth(170)
        self.service.currentIndexChanged.connect(self._service_picked)

        self.search = QLineEdit()
        self.search.setObjectName("browserSearch")
        self.search.setPlaceholderText("Search songs, artists, albums, playlists…")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(lambda: self.run_search(self.search.text(), None))

        self.link_btn = QPushButton("Link account…")
        self.link_btn.setObjectName("accentButton")
        self.link_btn.clicked.connect(lambda: self.link_requested.emit(self.name))
        self.link_btn.hide()

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(self.back_btn)
        top.addWidget(self.home_btn)
        top.addWidget(self.service)
        top.addWidget(self.search, 1)
        top.addWidget(self.link_btn)

        self.chips = QHBoxLayout()
        self.chips.setSpacing(6)
        self.chip_group = QButtonGroup(self)
        self.chip_group.setExclusive(True)
        self.chip_row = QWidget()
        chip_lay = QHBoxLayout(self.chip_row)
        chip_lay.setContentsMargins(0, 0, 0, 0)
        chip_lay.addLayout(self.chips)
        chip_lay.addStretch(1)

        self.crumbs = QLabel()
        self.crumbs.setObjectName("crumbs")
        self.crumbs.setTextFormat(Qt.TextFormat.RichText)
        self.crumbs.linkActivated.connect(lambda i: self._goto_crumb(int(i)))

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setObjectName("browserScroll")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        root.addLayout(top)
        root.addWidget(self.chip_row)
        root.addWidget(self.crumbs)
        root.addWidget(self.scroll, 1)
        self._message("Pick a speaker to browse its music services.")

    # -- public ------------------------------------------------------------

    @property
    def name(self) -> str:
        return self.service.currentText()

    def load_services(self, catalogue: list, linked: set[str], preferred: str = "") -> None:
        """Fill the service list in three groups: accounts you have linked,
        services that need no account, and ones that need linking first.

        Dozens of free radio services need no account, so a flat "works now"
        group would bury Spotify among them.
        """
        self._catalogue = catalogue
        groups = (
            [s for s in catalogue if s.needs_link and s.name in linked],
            [s for s in catalogue if not s.needs_link],
            [s for s in catalogue if s.needs_link and s.name not in linked],
        )
        self.service.blockSignals(True)
        self.service.clear()
        for group in groups:
            if not group:
                continue
            if self.service.count():
                self.service.insertSeparator(self.service.count())
            for s in group:
                self.service.addItem(s.name, s)
        yours, free, _ = groups
        fallback = yours[0].name if yours else \
            next((s.name for s in free if s.name == "TuneIn"), free[0].name if free else "")
        # An empty name would match a separator, whose text is empty too.
        want = preferred if preferred and self.service.findText(preferred) >= 0 else fallback
        index = self.service.findText(want) if want else -1
        self.service.setCurrentIndex(index if index >= 0 else 0)
        self.service.blockSignals(False)
        self._service_picked()

    def restyle(self) -> None:
        """Redraw what carries colours baked in, after a theme change."""
        self._render_crumbs()
        for view in self._views:
            view.viewport().update()

    def set_now_playing(self, uri: str) -> None:
        now = track_id_from_uri(uri)
        if now != self._now_id:
            self._now_id = now
            for view in self._views:
                if isinstance(view, TrackList):
                    view.viewport().update()

    def refresh_playlists(self) -> None:
        sp = self.speaker()
        if sp is not None:
            workers.run(self.services.sonos_playlists, sp,
                        on_done=lambda pls: setattr(self, "_playlists", pls),
                        on_error=lambda _e: None)

    # -- navigation --------------------------------------------------------

    def home(self) -> None:
        self._history.clear()
        self._go(Location("root", self.name))

    def back(self) -> None:
        if len(self._history) > 1:
            self._history.pop()
            self._go(self._history[-1], push=False)

    def open_entry(self, entry: Entry) -> None:
        self._go(Location("open", entry.title, entry=entry, ref=entry.item))

    def open_id(self, item_id: str, title: str) -> None:
        self._go(Location("open", title, ref=item_id))

    def run_search(self, term: str, category: str | None) -> None:
        term = term.strip()
        if not term:
            return
        category = category or ("all" if "all" in self._categories else
                                (self._categories[0] if self._categories else ""))
        # Searching again from a results page replaces it rather than stacking.
        if self._history and self._history[-1].kind == "search":
            self._history.pop()
        self._go(Location("search", f"“{term}”", term=term, category=category))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.BackButton:
            self.back()
        else:
            super().mousePressEvent(event)

    def _go(self, loc: Location, push: bool = True) -> None:
        sp = self.speaker()
        if sp is None:
            self._message("Pick a speaker to browse its music services.")
            return
        if push:
            self._history.append(loc)
        if loc.kind == "search" and self.search.text() != loc.term:
            self.search.setText(loc.term)  # e.g. after Back, or a chip
        self._render_crumbs()
        self.back_btn.setEnabled(len(self._history) > 1)
        self._show_chips(loc)
        self._message("Loading…", faint=True)
        self._serial += 1
        serial = self._serial
        name = self.name

        def job():
            if loc.kind == "root":
                root, sections = self.services.home_page(name, sp)
                return [describe(i) for i in root], \
                    [(describe(f), [describe(i) for i in items]) for f, items in sections]
            if loc.kind == "search":
                return [describe(i) for i in self.services.search(
                    name, sp, loc.category, loc.term, count=50)], []
            entry = loc.entry
            if entry is None:  # opened by id: look the item up for its header
                entry = describe(self.services.lookup(name, sp, loc.ref))
                loc.entry, loc.ref = entry, entry.item
            if entry.kind == "artist":
                children, top = self.services.artist_page(name, sp, entry.item)
                return [describe(i) for i in children], [describe(i) for i in top]
            return [describe(i) for i in self.services.browse(name, sp, entry.item, count=200)], []

        def done(result) -> None:
            if serial != self._serial:
                return  # the user moved on before this arrived
            entries, extra = result
            self._render_crumbs()
            self._render(loc, entries, extra)

        def failed(message: str) -> None:
            if serial == self._serial:
                self._message("That did not load.", message)
                self.error.emit(message)

        workers.run(job, on_done=done, on_error=failed)

    def _goto_crumb(self, index: int) -> None:
        if 0 <= index < len(self._history):
            del self._history[index + 1:]
            self._go(self._history[index], push=False)

    def _render_crumbs(self) -> None:
        parts = []
        for i, loc in enumerate(self._history):
            label = (loc.title or "?").replace("&", "&amp;").replace("<", "&lt;")
            if loc.kind == "search":
                label = f"Search {label}"
            if i == len(self._history) - 1:
                parts.append(f'<span style="color:{style.C["text"]}">{label}</span>')
            else:
                parts.append(f'<a href="{i}" style="color:{style.C["muted"]};'
                             f'text-decoration:none">{label}</a>')
        sep = f' <span style="color:{style.C["faint"]}">›</span> '
        self.crumbs.setText(sep.join(parts))

    def _show_chips(self, loc: Location) -> None:
        for b in self.chip_group.buttons():
            self.chip_group.removeButton(b)
            b.deleteLater()
        if loc.kind != "search" or len(self._categories) < 2:
            self.chip_row.hide()
            return
        ordered = sorted(self._categories, key=lambda c: (
            ["all", "tracks", "artists", "albums", "playlists"].index(c)
            if c in ("all", "tracks", "artists", "albums", "playlists") else 9))
        for cat in ordered:
            chip = QPushButton(cat.capitalize() if cat != "all" else "Everything")
            chip.setObjectName("chip")
            chip.setCheckable(True)
            chip.setChecked(cat == loc.category)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(lambda _c=False, c=cat: self.run_search(loc.term, c))
            self.chip_group.addButton(chip)
            self.chips.addWidget(chip)
        self.chip_row.show()

    # -- service selection -------------------------------------------------

    def _service_picked(self, *_args) -> None:
        name = self.name
        sp = self.speaker()
        if not name or sp is None:
            return
        self.service_changed.emit(name)
        self._history.clear()
        self._message("Loading…", faint=True)

        def job():
            linked = self.services.is_linked(name, sp)
            return linked, (self.services.categories(name, sp) if linked else [])

        def done(result) -> None:
            linked, cats = result
            self._categories = cats
            self.link_btn.setVisible(not linked)
            self.search.setEnabled(linked and bool(cats))
            if linked:
                self.home()
            else:
                self.crumbs.setText("")
                self._message(f"Link {name} to browse it.",
                              f"{name} needs an account. Linking opens {name}'s own login "
                              "page in your browser; your password never passes through "
                              "this app.", button=("Link account…",
                                                   lambda: self.link_requested.emit(name)))

        workers.run(job, on_done=done, on_error=lambda m: self._message("That did not load.", m))

    # -- rendering ---------------------------------------------------------

    def _set_page(self, page: QWidget) -> None:
        self._views = page.findChildren(_FittedView)
        old = self.scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self.scroll.setWidget(page)
        self.scroll.verticalScrollBar().setValue(0)

    def _message(self, title: str, detail: str = "", faint: bool = False,
                 button: tuple[str, Callable] | None = None) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addStretch(1)
        head = QLabel(title)
        head.setObjectName("emptyTitle" if not faint else "hint")
        head.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(head)
        if detail:
            body = QLabel(detail)
            body.setObjectName("hint")
            body.setWordWrap(True)
            body.setAlignment(Qt.AlignmentFlag.AlignCenter)
            body.setMaximumWidth(520)
            lay.addWidget(body, 0, Qt.AlignmentFlag.AlignHCenter)
        if button:
            b = QPushButton(button[0])
            b.setObjectName("accentButton")
            b.clicked.connect(button[1])
            lay.addSpacing(12)
            lay.addWidget(b, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addStretch(2)
        self._set_page(page)

    def _now(self) -> str:
        return self._now_id

    def _wire(self, view: _FittedView) -> _FittedView:
        view.activated_at.connect(self._activate_at)
        view.menu_requested.connect(self._menu)
        return view

    def _tracks(self, entries: list[Entry], show_art: bool = True) -> TrackList:
        return self._wire(TrackList(entries, self.loader, show_art, self._now))

    def _cards(self, entries: list[Entry]) -> CardGrid:
        return self._wire(CardGrid(entries, self.loader, lambda e: self.play([e])))

    def _render(self, loc: Location, entries: list[Entry], extra: list[Entry]) -> None:
        page = QWidget()
        page.setObjectName("browserPage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 0, 12, 24)
        lay.setSpacing(0)

        if loc.kind == "open" and loc.entry is not None:
            lay.addWidget(self._hero(loc.entry, entries, extra))

        if loc.kind == "root":
            self._home_body(lay, entries, extra)
        elif not entries and not extra:
            empty = QLabel("Nothing here." if loc.kind != "search"
                           else f"Nothing found for {loc.title}.")
            empty.setObjectName("emptyTitle")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addSpacing(60)
            lay.addWidget(empty)
        elif loc.kind == "search" and loc.category == "all":
            self._sectioned(lay, entries, loc)
        elif loc.entry is not None and loc.entry.kind == "artist":
            self._artist_body(lay, loc.entry, entries, extra)
        else:
            album = loc.entry is not None and loc.entry.kind == "album"
            self._grouped(lay, entries, numbered_album=album)
        lay.addStretch(1)
        self._set_page(page)

    def _home_body(self, lay: QVBoxLayout, root: list[Entry],
                   library: list[tuple[Entry, list[Entry]]]) -> None:
        """The service's front page: the listener's own music first.

        Services bury the user's playlists and albums under a library folder,
        so the home page opens that folder and shows its contents directly,
        with personal mixes such as Discover Weekly pulled to the top.
        """
        personal = [e for _f, items in library for e in items if is_personal(e)]
        if personal:
            lay.addWidget(_section("Made for you", self._cards(personal)))
        for folder, items in library:
            items = [e for e in items if not is_personal(e)]
            if not items:
                continue
            title = f"Your {folder.title.lower()}"
            if all(e.kind == "track" for e in items):
                body, limit = self._tracks(items[:5]), 5
            else:
                body, limit = self._cards(items[:14]), 14
            show_all = (lambda f=folder: self.open_entry(f)) if len(items) > limit else None
            lay.addWidget(_section(title, body, show_all))
        browse = [e for e in root
                  if e.title.strip().lower() not in Services.LIBRARY_NAMES or not library]
        if browse:
            lay.addWidget(_section("Browse" if library else "", self._cards(browse)))

    def _sectioned(self, lay: QVBoxLayout, entries: list[Entry], loc: Location) -> None:
        for kind, title, category, limit in SEARCH_SECTIONS:
            group = [e for e in entries if e.kind == kind]
            if not group:
                continue
            show_all = (lambda c=category: self.run_search(loc.term, c)) \
                if category and category in self._categories and len(group) > limit else None
            body = self._tracks(group[:limit]) if kind == "track" else self._cards(group[:limit])
            lay.addWidget(_section(title, body, show_all))

    def _grouped(self, lay: QVBoxLayout, entries: list[Entry], numbered_album: bool = False) -> None:
        tracks = [e for e in entries if e.kind == "track"]
        if numbered_album and tracks:
            # The album and its artist are in the header already; repeating them
            # on every row is noise. Guest artists still get named.
            lead = tracks[0].artist
            tracks = [replace(t, subtitle=t.artist if t.artist != lead else "") for t in tracks]
        if tracks:
            lay.addWidget(_section("Songs" if not numbered_album else "",
                                   self._tracks(tracks, show_art=not numbered_album)))
        for kind, title, _cat, _limit in SEARCH_SECTIONS:
            if kind == "track":
                continue
            group = [e for e in entries if e.kind == kind]
            if group:
                label = title if len({e.kind for e in entries}) > 1 or kind != "folder" else ""
                lay.addWidget(_section(label, self._cards(group)))

    def _artist_body(self, lay: QVBoxLayout, artist: Entry, entries: list[Entry],
                     top: list[Entry]) -> None:
        top_folder = next((e for e in entries if e.kind == "folder"
                           and "top" in e.title.lower()), None)
        if top:
            show_all = (lambda f=top_folder: self.open_entry(f)) if top_folder else None
            lay.addWidget(_section("Popular", self._tracks(top), show_all))
        radio = [e for e in entries if e.kind in ("program", "stream")]
        if radio:
            lay.addWidget(_section("Radio", self._cards(radio)))
        albums = [e for e in entries if e.kind == "album"]
        if albums:
            lay.addWidget(_section(f"Albums · {len(albums)}", self._cards(albums)))
        rest = [e for e in entries if e.kind in ("playlist", "folder", "artist")
                and e is not top_folder]
        if rest:
            lay.addWidget(_section("More", self._cards(rest)))

    def _hero(self, entry: Entry, entries: list[Entry], extra: list[Entry]) -> Hero:
        tracks = [e for e in entries if e.kind == "track"]
        bits: list[str] = []
        if entry.kind == "artist":
            albums = sum(1 for e in entries if e.kind == "album")
            if albums:
                bits.append(f"{albums} albums")
            if any(e.kind == "program" for e in entries):
                bits.append("radio available")
        else:
            if entry.artist and entry.kind == "album":
                bits.append(entry.artist)
            elif entry.subtitle and entry.kind != "folder":
                bits.append(entry.subtitle)
            if tracks:
                bits.append(f"{len(tracks)} songs")
                total = sum(e.duration for e in tracks)
                if total:
                    bits.append(fmt_duration(total, long=True))
            elif entries:
                bits.append(f"{len(entries)} items")

        buttons: list[QPushButton] = []
        if entry.kind == "artist" and extra:
            play = QPushButton("▶  Play popular")
            play.setObjectName("accentButton")
            play.clicked.connect(lambda: self.play(extra))
            buttons.append(play)
            radio = next((e for e in entries if e.kind == "program"), None)
            if radio is not None:
                b = QPushButton("Radio")
                b.setObjectName("pillButton")
                b.clicked.connect(lambda: self.play([radio]))
                buttons.append(b)
        elif entry.playable:
            play = QPushButton("▶  Play")
            play.setObjectName("accentButton")
            play.clicked.connect(lambda: self.play([entry]))
            buttons.append(play)
            if entry.queueable:
                queue = QPushButton("＋  Add to queue")
                queue.setObjectName("pillButton")
                queue.clicked.connect(lambda: self.enqueue([entry]))
                buttons.append(queue)
                to_list = QPushButton("♫  Add to playlist")
                to_list.setObjectName("pillButton")
                to_list.clicked.connect(
                    lambda: self._playlist_menu([entry]).exec(
                        to_list.mapToGlobal(to_list.rect().bottomLeft())))
                buttons.append(to_list)
        return Hero(entry, "  ·  ".join(bits), self.loader, buttons)

    def _art_landed(self, url: str) -> None:
        for view in self._views:
            if any(e.art == url for e in view.entries):
                view.viewport().update()

    # -- actions -----------------------------------------------------------

    def _activate_at(self, entries: list[Entry], row: int) -> None:
        entry = entries[row]
        if entry.kind == "track":
            self.play_from(entries, row)
        elif entry.kind == "stream":
            self.play([entry])
        elif entry.openable:
            self.open_entry(entry)
        elif entry.playable:
            self.play([entry])

    def _run(self, fn, *args, ok: str, then=None) -> None:
        sp = self.speaker()
        if sp is None:
            self.error.emit("Pick a speaker first.")
            return

        def done(_r) -> None:
            self.status.emit(ok)
            if then:
                then()

        workers.run(fn, *args, on_done=done, on_error=self.error.emit)

    def play(self, entries: list[Entry]) -> None:
        """Play exactly these, from the first, replacing the queue."""
        self.play_from(entries, 0)

    def play_from(self, entries: list[Entry], row: int) -> None:
        """Play `entries[row]` and carry on down the rest of the list."""
        sp = self.speaker()
        self._run(self.services.play_context, self.name, sp, [e.item for e in entries], row,
                  ok=f"Playing {entries[row].title}.", then=self.played.emit)

    def enqueue(self, entries: list[Entry], next_up: bool = False) -> None:
        sp = self.speaker()
        label = entries[0].title if len(entries) == 1 else f"{len(entries)} items"
        self._run(lambda: self.services.enqueue(sp, [e.item for e in entries], next_up=next_up),
                  ok=f"{label} will play next." if next_up else f"Added {label} to the queue.",
                  then=self.played.emit)

    def add_to_playlist(self, entries: list[Entry], playlist) -> None:
        sp = self.speaker()
        self._run(self.services.add_to_playlist, sp, playlist, [e.item for e in entries],
                  ok=f"Added to “{playlist.title}”.", then=self._playlists_touched)

    def new_playlist(self, entries: list[Entry]) -> None:
        default = entries[0].title if len(entries) == 1 and entries[0].kind != "track" else ""
        title, ok = QInputDialog.getText(self, "New playlist", "Name:", text=default)
        if not ok or not title.strip():
            return
        sp = self.speaker()
        self._run(self.services.new_playlist, sp, title.strip(), [e.item for e in entries],
                  ok=f"Created “{title.strip()}”.", then=self._playlists_touched)

    def _playlists_touched(self) -> None:
        self.refresh_playlists()
        self.playlists_changed.emit()

    def _playlist_menu(self, entries: list[Entry]) -> QMenu:
        menu = QMenu("♫  Add to playlist", self)
        menu.addAction("＋  New playlist…").triggered.connect(
            lambda: self.new_playlist(entries))
        if self._playlists:
            menu.addSeparator()
            for pl in self._playlists:
                menu.addAction(pl.title).triggered.connect(
                    lambda _c=False, p=pl: self.add_to_playlist(entries, p))
        return menu

    def _menu(self, entries: list[Entry], pos, context: list[Entry] | None = None) -> None:
        menu = self.build_menu(entries, context)
        if not menu.isEmpty():
            menu.exec(pos)

    def build_menu(self, entries: list[Entry], context: list[Entry] | None = None) -> QMenu:
        """The right-click menu for one or more results.

        `context` is the whole list the selection came from. Playing a single
        song from it plays on down that list, the same as double-clicking it.
        """
        first = entries[0]
        menu = QMenu(self)
        if any(e.playable for e in entries):
            if len(entries) == 1 and first.kind == "track" and context and first in context:
                play = lambda: self.play_from(context, context.index(first))  # noqa: E731
            else:
                play = lambda: self.play(entries)  # noqa: E731
            menu.addAction("▶  Play").triggered.connect(play)
        queueable = [e for e in entries if e.queueable]
        if queueable:
            menu.addAction("⏭  Play next").triggered.connect(
                lambda: self.enqueue(queueable, next_up=True))
            menu.addAction("＋  Add to queue").triggered.connect(
                lambda: self.enqueue(queueable))
            menu.addMenu(self._playlist_menu(queueable))
        if len(entries) == 1:
            nav = []
            if first.openable:
                nav.append(("Open", lambda: self.open_entry(first)))
            if first.kind in ("track", "album") and first.artist_id:
                nav.append((f"Go to artist · {first.artist}",
                            lambda: self.open_id(first.artist_id, first.artist)))
            if first.kind == "track" and first.album_id:
                nav.append((f"Go to album · {first.album}",
                            lambda: self.open_id(first.album_id, first.album)))
            if nav:
                menu.addSeparator()
                for text, fn in nav:
                    menu.addAction(text).triggered.connect(fn)
        return menu
