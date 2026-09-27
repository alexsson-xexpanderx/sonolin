"""The themes dialog against a stand-in window: live preview, save, discard."""

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from nosonpy.gui import themes  # noqa: E402
from nosonpy.gui.theme_dialog import ThemeDialog  # noqa: E402


class Window:
    """Records what the dialog asks the real window to do."""

    def __init__(self):
        self.applied = []

    def apply_theme(self, theme, remember=True):
        self.applied.append((theme.name, theme.colors["accent"], remember))


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _dialog(app, name="Dark"):
    win = Window()
    dialog = ThemeDialog(None, themes.find(name))
    dialog.window_ = win
    return dialog, win


def test_picking_a_theme_applies_and_remembers_it(app):
    d, win = _dialog(app)
    row = next(i for i, t in enumerate(d._all) if t.name == "Synthwave")
    d.list.setCurrentRow(row)
    assert win.applied[-1] == ("Synthwave", themes.find("Synthwave").colors["accent"], True)
    assert d.delete_btn.isEnabled() is False, "built-ins cannot be deleted"


def test_editing_previews_live_without_remembering(app):
    d, win = _dialog(app)
    d.hexes["accent"].setText("#ff6600")
    d._hex_edited("accent")
    assert win.applied[-1] == ("Dark", "#ff6600", False)
    assert d.dirty and "unsaved" in d.windowTitle()
    d.hexes["accent"].setText("not a colour")
    d._hex_edited("accent")
    assert d.hexes["accent"].text() == "#ff6600", "a bad value is put back"


def test_discarding_puts_the_saved_theme_back(app, monkeypatch):
    d, win = _dialog(app)
    d.working.colors["accent"] = "#123456"
    d._changed()
    # Qt orders message-box buttons by role, so find it by its label.
    monkeypatch.setattr(QMessageBox, "exec", lambda self: next(
        b for b in self.buttons() if b.text() == "Discard").click() or 0)
    assert d._resolve_unsaved() is True
    assert win.applied[-1] == ("Dark", themes.DARK["accent"], True)


def test_save_as_new_makes_it_yours(app, monkeypatch):
    from PyQt6.QtWidgets import QInputDialog

    d, win = _dialog(app)
    d.working.colors["accent"] = "#00cc88"
    d._changed()
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Minty", True))
    d._save_as()
    mine = themes.find("Minty")
    assert mine is not None and not mine.builtin and mine.colors["accent"] == "#00cc88"
    assert not d.dirty and d.delete_btn.isEnabled()
    assert win.applied[-1] == ("Minty", "#00cc88", True)


# -- the real buttons, not the methods behind them -------------------------------
# Close once stayed open: reject() called close(), whose close event called
# reject() again, and Qt's re-entry guard left the dialog showing.

from PyQt6.QtCore import QEvent, Qt  # noqa: E402
from PyQt6.QtGui import QKeyEvent  # noqa: E402
from PyQt6.QtWidgets import QPushButton  # noqa: E402


def _button(d, text):
    return next(b for b in d.findChildren(QPushButton) if b.text() == text)


def _answer(monkeypatch, label):
    monkeypatch.setattr(QMessageBox, "exec", lambda self: next(
        b for b in self.buttons() if b.text() == label).click() or 0)


def _shown(app, name="Dark"):
    d, win = _dialog(app, name)
    d.show()
    app.processEvents()
    return d, win


def test_close_button_closes(app):
    d, _ = _shown(app)
    _button(d, "Close").click()
    app.processEvents()
    assert not d.isVisible()


def test_escape_and_the_window_close_button_close_too(app):
    d, _ = _shown(app)
    QApplication.sendEvent(d, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                        Qt.KeyboardModifier.NoModifier))
    app.processEvents()
    assert not d.isVisible()
    d, _ = _shown(app)
    d.close()  # what the title bar's × does
    app.processEvents()
    assert not d.isVisible()


def test_unsaved_edits_can_keep_you_in_the_dialog(app, monkeypatch):
    d, _ = _shown(app)
    d.working.colors["accent"] = "#abcdef"
    d._changed()
    _answer(monkeypatch, "Keep editing")
    _button(d, "Close").click()
    app.processEvents()
    assert d.isVisible() and d.dirty


def test_discarding_on_close_reverts_and_closes(app, monkeypatch):
    d, win = _shown(app)
    d.working.colors["accent"] = "#abcdef"
    d._changed()
    _answer(monkeypatch, "Discard")
    _button(d, "Close").click()
    app.processEvents()
    assert not d.isVisible()
    assert win.applied[-1] == ("Dark", themes.DARK["accent"], True)


def test_save_button_writes_your_theme(app):
    mine = themes.save_user(themes.Theme("Nord (mine)", "dark",
                                         dict(themes.find("Nord").colors), builtin=False))
    d, _ = _shown(app, "Nord (mine)")
    assert not d.save_btn.isEnabled() and "Nothing has changed" in d.save_btn.toolTip()
    d.hexes["accent"].setText("#ff6600")
    d._hex_edited("accent")
    d.save_btn.click()
    assert themes.find("Nord (mine)").colors["accent"] == "#ff6600"
    assert not d.dirty
    _button(d, "Close").click()
    app.processEvents()
    assert not d.isVisible()
    themes.delete_user(themes.find("Nord (mine)"))
    assert mine.path and not mine.path.exists()
