"""The light/dark toggle on the real window, with no speaker and no polling."""

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_toggle_returns_to_the_favourite_on_each_side(app):
    from nosonpy.controller import Controller
    from nosonpy.gui import style, themes
    from nosonpy.gui.app import MainWindow

    c = Controller([])
    w = MainWindow(c)
    w.timer.stop()
    try:
        w.apply_theme(themes.find("Synthwave"))
        assert (c.config.theme, c.config.theme_dark) == ("Synthwave", "Synthwave")
        w._toggle_mode()
        assert c.config.theme == "Light" and style.C["bg"] == themes.LIGHT["bg"]
        assert "Dark" in w.mode_action.text()
        w._toggle_mode()
        assert c.config.theme == "Synthwave" and style.C["accent"] == "#ff3ea5"
    finally:
        w.mpris.unregister()
        style.apply(app, themes.DARK)
