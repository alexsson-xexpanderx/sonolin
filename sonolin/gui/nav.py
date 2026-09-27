"""The side bar's page menu: what used to be the row of tabs.

Each page gets a line icon drawn here as a small SVG and tinted with the theme,
rather than an emoji: emoji come from whatever colour font the desktop has, at
whatever size it likes, and at 16 px a speaker glyph can pass for a microphone.
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QByteArray, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QAbstractItemView, QListWidget, QListWidgetItem, QStyle, QStyledItemDelegate,
)

from . import style

#: 24×24 line drawings, stroked in `currentColor`.
ICONS = {
    "queue": '<path d="M3 6h13M3 12h13M3 18h8"/><path d="M16 15v6l5-3z"/>',
    "library": '<rect x="3" y="4" width="4" height="16" rx="1"/>'
               '<rect x="9" y="4" width="4" height="16" rx="1"/>'
               '<path d="M15.2 5.6l3.9-1 4 15.4-3.9 1z"/>',
    "browse": '<circle cx="12" cy="12" r="9"/><path d="M15.5 8.5l-2 5-5 2 2-5z"/>',
    "favourites": '<path d="M12 3.5l2.6 5.3 5.9.9-4.2 4.1 1 5.8-5.3-2.8-5.3 2.8 1-5.8'
                  '-4.2-4.1 5.9-.9z"/>',
    "sound": '<path d="M6 4v16M12 4v16M18 4v16"/><path d="M3.5 14h5M9.5 8h5M15.5 16h5"/>',
    "alarms": '<circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 2.5"/>'
              '<path d="M3.5 6L7 3.5M20.5 6L17 3.5"/>',
    "announce": '<path d="M3 10v4h4l6 4V6l-6 4z"/>'
                '<path d="M16.5 9.5a3.5 3.5 0 0 1 0 5M19 7a7 7 0 0 1 0 10"/>',
    "stream": '<rect x="2.5" y="4" width="19" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>'
              '<path d="M9.5 12.5a3.5 3.5 0 0 1 5 0M7.5 10a6.5 6.5 0 0 1 9 0"/>',
    "device": '<rect x="6" y="2.5" width="12" height="19" rx="2.5"/>'
              '<circle cx="12" cy="14.5" r="3.2"/><circle cx="12" cy="7" r="1"/>',
}



def _gear(teeth: int = 8, outer: float = 9.6, inner: float = 7.2) -> str:
    """A cog outline around a hub, worked out rather than drawn by hand."""
    points = []
    for i in range(teeth):
        centre = 360 / teeth * i
        for angle, radius in ((-15, inner), (-8.5, outer), (8.5, outer), (15, inner)):
            a = math.radians(centre + angle)
            points.append(f"{12 + radius * math.cos(a):.2f} {12 + radius * math.sin(a):.2f}")
    return f'<path d="M{"L".join(points)}z"/><circle cx="12" cy="12" r="3"/>'


def _sun() -> str:
    rays = "".join(
        f"M{12 + 7 * math.cos(a):.2f} {12 + 7 * math.sin(a):.2f}"
        f"L{12 + 9.5 * math.cos(a):.2f} {12 + 9.5 * math.sin(a):.2f}"
        for a in (math.radians(45 * i) for i in range(8)))
    return f'<circle cx="12" cy="12" r="4"/><path d="{rays}"/>'


ICONS["settings"] = _gear()
ICONS["sun"] = _sun()
ICONS["moon"] = '<path d="M20 14.2A8.2 8.2 0 1 1 9.8 4a6.6 6.6 0 0 0 10.2 10.2z"/>'

HEADER_ROLE = Qt.ItemDataRole.UserRole + 20
ICON_ROLE = Qt.ItemDataRole.UserRole + 21
PAGE_ROLE = Qt.ItemDataRole.UserRole + 22

_cache: dict[tuple[str, str, int, float], QPixmap] = {}


def icon(name: str, colour: str, size: int) -> QPixmap:
    """`name`'s drawing at `size` logical pixels, sharp on high-DPI screens."""
    screen = QGuiApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen is not None else 1.0
    key = (name, colour, size, dpr)
    if key not in _cache:
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
               f'stroke="{colour}" stroke-width="1.8" stroke-linecap="round" '
               f'stroke-linejoin="round">{ICONS[name]}</svg>')
        px = max(1, round(size * dpr))
        pix = QPixmap(px, px)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(0, 0, px, px))
        painter.end()
        pix.setDevicePixelRatio(dpr)
        _cache[key] = pix
    return _cache[key]


class NavDelegate(QStyledItemDelegate):
    """Section headings in small capitals; pages as an icon and a name."""

    ROW, HEADER, FIRST_HEADER = 36, 34, 26
    ICON = 18

    def sizeHint(self, option, index) -> QSize:
        if index.data(HEADER_ROLE):
            return QSize(option.rect.width(), self.FIRST_HEADER if index.row() == 0
                         else self.HEADER)
        return QSize(option.rect.width(), self.ROW)

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect)
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        if index.data(HEADER_ROLE):
            font = QFont(option.font)
            font.setPointSizeF(8.5)
            font.setBold(True)
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
            painter.setFont(font)
            painter.setPen(QColor(style.C["faint"]))
            painter.drawText(rect.adjusted(11, 0, 0, -6),  # in line with "SPEAKERS"
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, text)
            painter.restore()
            return

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        card = rect.adjusted(0, 2, 0, -2)
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(style.C["accent_lo"] if selected else style.C["raised"]))
            painter.drawRoundedRect(card, 9, 9)
        if selected:  # a bar at the left edge, like the tab underline it replaces
            painter.setBrush(QColor(style.C["accent"]))
            painter.drawRoundedRect(QRectF(card.left(), card.top() + 8, 3, card.height() - 16),
                                    1.5, 1.5)
        colour = style.C["selection_text"] if selected else \
            style.C["text"] if hovered else style.C["muted"]
        pix = icon(index.data(ICON_ROLE), style.C["accent"] if selected else colour, self.ICON)
        top = card.center().y() - self.ICON / 2
        painter.drawPixmap(QRectF(card.left() + 12, top, self.ICON, self.ICON), pix,
                           QRectF(pix.rect()))
        font = QFont(option.font)
        font.setBold(selected)
        painter.setFont(font)
        painter.setPen(QColor(colour))
        painter.drawText(card.adjusted(12 + self.ICON + 12, 0, -8, 0),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight,
                                                          int(card.width() - 50)))
        painter.restore()


class NavList(QListWidget):
    """The page menu. `page_chosen(index)` gives the page's place in the stack."""

    page_chosen = pyqtSignal(int)

    def __init__(self, sections: list[tuple[str, list[tuple[str, str]]]]) -> None:
        """`sections` is ``[(heading, [(page name, icon name), …]), …]``."""
        super().__init__()
        self.setObjectName("navList")
        self.setItemDelegate(NavDelegate(self))
        self.setMouseTracking(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._rows: list[int] = []  # list row of each page, in page order
        for heading, pages in sections:
            header = QListWidgetItem(heading)
            header.setData(HEADER_ROLE, True)
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            self.addItem(header)
            for name, icon_name in pages:
                item = QListWidgetItem(name)
                item.setData(ICON_ROLE, icon_name)
                item.setData(PAGE_ROLE, len(self._rows))
                item.setToolTip(name)
                self._rows.append(self.count())
                self.addItem(item)
        self.currentRowChanged.connect(self._row_changed)
        # Tall enough for every entry: the menu never scrolls, the speakers do.
        self.setFixedHeight(sum(self.sizeHintForRow(r) for r in range(self.count())) + 12)

    def _row_changed(self, row: int) -> None:
        page = self.item(row).data(PAGE_ROLE) if row >= 0 else None
        if page is not None:
            self.page_chosen.emit(page)

    def page(self) -> int:
        item = self.currentItem()
        return item.data(PAGE_ROLE) if item is not None else -1

    def set_page(self, page: int) -> None:
        self.setCurrentRow(self._rows[page])
