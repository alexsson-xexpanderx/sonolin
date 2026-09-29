"""The light/dark toggle on the real window, with no speaker and no polling."""

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """The window looks for speakers and scans music shortly after it opens;
    a test that lets the event loop run must not reach the real network."""
    from sonolin.gui.app import MainWindow

    monkeypatch.setattr(MainWindow, "_discover", lambda self: None)
    monkeypatch.setattr(MainWindow, "_scan", lambda self: None)


def test_toggle_returns_to_the_favourite_on_each_side(app):
    from sonolin.controller import Controller
    from sonolin.gui import style, themes
    from sonolin.gui.app import MainWindow

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


def test_the_media_server_shows_only_while_it_runs(app):
    from types import SimpleNamespace

    from sonolin.controller import Controller
    from sonolin.gui.app import MainWindow

    c = Controller([])
    w = MainWindow(c)
    w.timer.stop()
    try:
        w._update_server_label()
        assert w.server_label.isHidden(), "nothing to say while it is off"
        c.server = SimpleNamespace(port=1405, base_url="http://10.0.0.5:1405")
        w._update_server_label()
        assert not w.server_label.isHidden()
        assert "http://10.0.0.5:1405" in w.server_label.text()
        assert w.statusBar().isAncestorOf(w.server_label), "in the status bar, not the toolbar"
    finally:
        c.server = None
        w.mpris.unregister()


def test_the_side_bar_menu_switches_pages(app):
    from PyQt6.QtGui import QKeySequence, QShortcut

    from sonolin.controller import Controller
    from sonolin.gui.app import PAGES, MainWindow

    c = Controller([])
    w = MainWindow(c)
    w.timer.stop()
    try:
        names = [name for _heading, pages in PAGES for name, _icon in pages]
        assert w.pages.count() == len(names)
        assert w.pages.currentWidget() is w.queue
        w.nav.set_page(names.index("Sound"))
        assert w.pages.currentWidget() is w.sound
        # Offscreen windows are never active, so keys would not reach the
        # shortcut; fire it directly.
        ctrl3 = next(sc for sc in w.findChildren(QShortcut)
                     if sc.key() == QKeySequence("Ctrl+3"))
        ctrl3.activated.emit()
        assert w.pages.currentWidget() is w.browser
        assert w.nav.currentItem().text() == "Browse"
    finally:
        w.mpris.unregister()


def test_there_is_no_toolbar_and_settings_holds_its_actions(app, monkeypatch):
    from types import SimpleNamespace

    from PyQt6.QtWidgets import QLabel, QToolBar

    from sonolin.controller import Controller
    from sonolin.gui import style, themes
    from sonolin.gui.app import MainWindow
    from sonolin.gui.settings_dialog import SettingsDialog

    c = Controller([])
    w = MainWindow(c)
    w.timer.stop()
    calls = []
    monkeypatch.setattr(w, "_discover", lambda: calls.append("discover"))
    monkeypatch.setattr(w, "_add_by_ip", lambda: calls.append("add"))
    monkeypatch.setattr(w, "_call", lambda name, **kw: calls.append(name))
    try:
        assert w.findChildren(QToolBar) == []
        d = SettingsDialog(w)
        assert not d.buttons["Group all here"].isEnabled(), "no speaker to group around"
        d.buttons["Find speakers"].click()
        d.buttons["Add by address…"].click()
        assert calls == ["discover", "add"]

        w.current = SimpleNamespace(name="Kitchen", awake=True)
        d = SettingsDialog(w)
        assert any("Kitchen" in label.text() for label in d.findChildren(QLabel))
        d.buttons["Group all here"].click()
        d.buttons["Leave group"].click()
        assert calls[-2:] == ["party_mode", "unjoin"]

        w.apply_theme(themes.find("Dark"))
        assert d.buttons["dark"].isChecked()
        d.buttons["light"].click()
        assert w.current_theme().base == "light" and d.buttons["light"].isChecked()
    finally:
        w.current = None
        w.mpris.unregister()
        style.apply(app, themes.DARK)


@pytest.mark.parametrize("speaker", [None, object()])
def test_stream_stop_revokes_desktop_session(app, speaker):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from sonolin.gui.app import MainWindow

    stop = Mock()
    window = SimpleNamespace(c=SimpleNamespace(stop_desktop=stop), current=speaker,
                             stream=SimpleNamespace(status=Mock()))
    def run(fn, *args, then):
        fn(*args)
        then()
    window._run = run
    MainWindow._stream_stop(window)
    stop.assert_called_once_with(speaker)
    window.stream.status.setText.assert_called_once_with("Not streaming.")
