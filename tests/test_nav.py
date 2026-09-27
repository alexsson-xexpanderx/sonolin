"""The side bar's page menu."""

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QColor  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from sonolin.gui.nav import ICONS, NavList, icon  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


SECTIONS = [("MUSIC", [("Queue", "queue"), ("Browse", "browse")]),
            ("SPEAKER", [("Sound", "sound")])]


def test_every_icon_draws_in_the_colour_asked_for(app):
    for name in ICONS:
        image = icon(name, "#ff0000", 18).toImage()
        inked = [QColor(image.pixel(x, y)) for x in range(image.width())
                 for y in range(image.height()) if QColor.fromRgba(image.pixel(x, y)).alpha()]
        assert inked, f"{name} drew nothing"
        assert max(c.red() for c in inked) > 200 and max(c.green() for c in inked) < 60, name


def test_headings_cannot_be_chosen(app):
    nav = NavList(SECTIONS)
    assert [nav.item(r).text() for r in range(nav.count())] == \
        ["MUSIC", "Queue", "Browse", "SPEAKER", "Sound"]
    assert nav.item(0).flags() == Qt.ItemFlag.NoItemFlags
    assert nav.item(3).flags() == Qt.ItemFlag.NoItemFlags


def test_pages_are_numbered_past_the_headings(app):
    nav = NavList(SECTIONS)
    chosen = []
    nav.page_chosen.connect(chosen.append)
    nav.set_page(2)
    assert nav.currentItem().text() == "Sound" and nav.page() == 2
    nav.setCurrentRow(1)
    assert chosen == [2, 0]


def test_the_menu_never_scrolls(app):
    nav = NavList(SECTIONS)
    rows = sum(nav.sizeHintForRow(r) for r in range(nav.count()))
    assert nav.height() >= rows
