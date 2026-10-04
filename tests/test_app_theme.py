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


def test_now_playing_art_uses_application_destinations(app, monkeypatch):
    from types import SimpleNamespace

    from sonolin.controller import Controller
    from sonolin.gui import workers
    from sonolin.gui.app import MainWindow

    c = Controller([])
    w = MainWindow(c)
    w.timer.stop()
    calls = []
    monkeypatch.setattr(workers, "run", lambda *args, **kw: calls.append((args, kw)))
    monkeypatch.setattr(w, "_refresh_item", lambda: None)
    w.current = SimpleNamespace(uid="speaker", ip="192.168.1.20", awake=True)
    c.server = SimpleNamespace(port=1405, base_url="http://192.168.1.5:1405")
    try:
        w._apply_poll({"track": {"album_art": "http://untrusted.example/cover"},
                       "state": "STOPPED", "volume": 20, "mute": False})
        assert calls[-1][0] == (w._fetch_art, "http://untrusted.example/cover",
                               "192.168.1.20", "http://192.168.1.5:1405")
        assert calls[-1][1]["on_done"] == w.now.set_art
    finally:
        c.server = None
        w.current = None
        w.mpris.unregister()


def test_now_playing_rejected_art_is_nonfatal():
    from sonolin.gui.app import MainWindow

    assert MainWindow._fetch_art("http://127.0.0.1/private", "192.168.1.20") is None


@pytest.mark.parametrize("has_speaker", [False, True])
def test_desktop_stop_revokes_access_without_a_reachable_speaker(app, monkeypatch, has_speaker):
    from types import SimpleNamespace

    from sonolin.controller import Controller
    from sonolin.gui.app import MainWindow
    from sonolin.mediaserver import MediaServer
    from sonolin.speaker import _Loop

    c = Controller([])
    c.server = MediaServer(host="127.0.0.1")
    _Loop.submit(c.server.start())
    _Loop.submit(c.server.start_stream())
    w = MainWindow(c)
    w.timer.stop()
    monkeypatch.setattr(w, "_run", lambda action, **kwargs: action())

    def unreachable():
        assert c.server._live_token is None
        raise OSError("speaker unreachable")

    w.current = SimpleNamespace(stop=unreachable) if has_speaker else None
    try:
        if has_speaker:
            with pytest.raises(OSError):
                w._stream_stop()
        else:
            w._stream_stop()
        assert c.server._live_token is None
    finally:
        w.current = None
        w.mpris.unregister()
        c.close()


def test_volume_follows_the_wheel_and_clicks_not_only_drags(app, monkeypatch):
    from types import SimpleNamespace

    from PyQt6.QtCore import QPoint, Qt
    from PyQt6.QtTest import QTest

    from sonolin.controller import Controller
    from sonolin.gui import app as app_module
    from sonolin.gui.app import MainWindow

    sent = []
    monkeypatch.setattr(app_module.workers, "run",
                        lambda fn, *args, **kw: sent.append(args) if fn is setattr else None)
    c = Controller([])
    w = MainWindow(c)
    w.timer.stop()
    try:
        w.current = SimpleNamespace(name="Kitchen")
        w.volume.setValue(20)
        for _ in range(8):  # a wheel spin: one send, of the last value
            w.volume.triggerAction(w.volume.SliderAction.SliderSingleStepAdd)
        QTest.qWait(w._volume_send.interval() + 100)
        assert sent == [(w.current, "volume", 28)]

        w.volume.resize(200, 24)
        w.volume.show()
        QTest.mouseClick(w.volume, Qt.MouseButton.LeftButton, pos=QPoint(190, 12))
        QTest.qWait(w._volume_send.interval() + 100)
        assert len(sent) == 2 and sent[-1][2] > 80

    finally:
        w.mpris.unregister()


def _window_with_jobs(monkeypatch):
    """A window whose background jobs are recorded instead of run."""
    from types import SimpleNamespace

    from sonolin.controller import Controller
    from sonolin.gui import app as app_module
    from sonolin.gui.app import MainWindow

    jobs = []
    monkeypatch.setattr(app_module.workers, "run", lambda fn, *a, **kw: jobs.append(fn))
    w = MainWindow(Controller([]))
    w.timer.stop()
    w.current = SimpleNamespace(uid="u1", name="Kitchen", awake=True,
                                play=lambda: None, pause=lambda: None)
    return w, jobs


def test_a_change_reported_during_a_poll_is_read_after_it(app, monkeypatch):
    from PyQt6.QtTest import QTest

    w, jobs = _window_with_jobs(monkeypatch)
    try:
        w._poll(force=True)
        w._poll(force=True)  # "playing" arrives while "buffering" is being read
        assert len(jobs) == 1
        w._poll_failed("")
        QTest.qWait(20)
        assert len(jobs) == 2

        # While buffering, the window looks every second, not every fifth.
        w._poll_failed("")
        w._states["u1"], w._ticks = "TRANSITIONING", 0
        w._tick()
        assert len(jobs) == 3
    finally:
        w.mpris.unregister()


def test_play_and_pause_answer_the_press_at_once(app, monkeypatch):
    w, jobs = _window_with_jobs(monkeypatch)
    try:
        w._toggle_play()
        assert w.play_btn.text() == "⏸" and w._states["u1"] == "TRANSITIONING"
        w.now._playing = True
        w._toggle_play()
        assert w.play_btn.text() == "▶" and not w.now._playing
        assert len(jobs) == 2  # play, then pause, still sent to the speaker
    finally:
        w.mpris.unregister()
