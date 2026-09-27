"""Pick, edit, import and export colour themes, with the app as the preview.

Choosing a theme applies it to the whole window at once, this dialog
included, so there is no separate preview to keep in step. Edits apply live
too, and are only kept once saved; closing with unsaved edits asks first.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PyQt6.QtCore import QRectF, QSize, Qt, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QColorDialog, QComboBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from . import themes
from .themes import HEX, ROLES, Theme, ThemeError


def swatch_icon(theme: Theme) -> QIcon:
    """A strip of the theme's main colours, for the list."""
    pix = QPixmap(72, 28)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    # A faint outline keeps a block visible when it matches the list behind it,
    # e.g. Nord's background while Nord is the active theme.
    edge = QColor(128, 128, 128, 110)
    p.setPen(edge)
    for i, key in enumerate(("bg", "surface", "accent", "text")):
        p.setBrush(QColor(theme.colors[key]))
        p.drawRoundedRect(QRectF(i * 18 + 0.5, 0.5, 16, 27), 4, 4)
    p.end()
    return QIcon(pix)


class ThemeDialog(QDialog):
    def __init__(self, window, current: Theme) -> None:
        super().__init__(window)
        self.window_ = window
        self.setWindowTitle("Themes")
        self.resize(900, 640)
        self.saved = current          # what is in force and stored
        self.working = replace(current, colors=dict(current.colors))
        self.dirty = False
        self._loading = False

        self.list = QListWidget()
        self.list.setIconSize(QSize(72, 28))
        self.list.setMinimumWidth(250)
        self.list.currentRowChanged.connect(self._picked)

        self.name = QLineEdit()
        self.name.textEdited.connect(self._name_edited)
        self.base = QComboBox()
        self.base.addItem("Dark", "dark")
        self.base.addItem("Light", "light")
        self.base.currentIndexChanged.connect(self._base_edited)
        self.note = QLabel()
        self.note.setObjectName("hint")
        self.note.setWordWrap(True)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        self.swatches: dict[str, QPushButton] = {}
        self.hexes: dict[str, QLineEdit] = {}
        for row, (key, label, about) in enumerate(ROLES):
            title = QLabel(label)
            desc = QLabel(about)
            desc.setObjectName("hint")
            sw = QPushButton()
            sw.setFixedSize(46, 26)
            sw.setCursor(Qt.CursorShape.PointingHandCursor)
            sw.setToolTip(f"Choose the {label.lower()} colour")
            sw.clicked.connect(lambda _c=False, k=key: self._pick_colour(k))
            hx = QLineEdit()
            hx.setFixedWidth(96)
            hx.editingFinished.connect(lambda k=key: self._hex_edited(k))
            self.swatches[key], self.hexes[key] = sw, hx
            grid.addWidget(sw, row, 0)
            grid.addWidget(hx, row, 1)
            grid.addWidget(title, row, 2)
            grid.addWidget(desc, row, 3)
        grid.setColumnStretch(3, 1)
        colours = QWidget()
        colours.setLayout(grid)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(colours)
        scroll.setObjectName("browserScroll")

        head = QHBoxLayout()
        head.addWidget(QLabel("Name"))
        head.addWidget(self.name, 1)
        head.addWidget(QLabel("Side"))
        head.addWidget(self.base)

        editor = QVBoxLayout()
        editor.addLayout(head)
        editor.addWidget(self.note)
        editor.addWidget(scroll, 1)

        body = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(self.list, 1)
        import_btn = QPushButton("Import…")
        import_btn.clicked.connect(self._import)
        export_btn = QPushButton("Export…")
        export_btn.clicked.connect(self._export)
        folder_btn = QPushButton("Open folder")
        folder_btn.setToolTip(str(themes.user_dir()))
        folder_btn.clicked.connect(self._open_folder)
        row = QHBoxLayout()
        row.addWidget(import_btn); row.addWidget(export_btn); row.addWidget(folder_btn)
        left.addLayout(row)
        body.addLayout(left)
        body.addLayout(editor, 1)

        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setObjectName("danger")
        self.delete_btn.clicked.connect(self._delete)
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self._save)
        save_as = QPushButton("Save as new…")
        save_as.clicked.connect(self._save_as)
        close = QPushButton("Close")
        close.setObjectName("accentButton")
        close.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.delete_btn)
        buttons.addStretch(1)
        buttons.addWidget(save_as); buttons.addWidget(self.save_btn); buttons.addWidget(close)

        root = QVBoxLayout(self)
        root.addLayout(body, 1)
        root.addLayout(buttons)

        self._reload_list(select=current.name)

    # -- list --------------------------------------------------------------

    def _reload_list(self, select: str) -> None:
        self._all = themes.all_themes()
        self.list.blockSignals(True)
        self.list.clear()
        for t in self._all:
            label = t.name + ("" if t.builtin else "   · yours")
            item = QListWidgetItem(swatch_icon(t), label)
            item.setToolTip(f"{t.name}\n{t.base} side" + ("" if t.builtin else f"\n{t.path}"))
            self.list.addItem(item)
        row = next((i for i, t in enumerate(self._all) if t.name == select), 0)
        self.list.setCurrentRow(row)
        self.list.blockSignals(False)
        self._load(self._all[row])

    def _picked(self, row: int) -> None:
        if not (0 <= row < len(self._all)):
            return
        if self.dirty and not self._resolve_unsaved():
            self.list.blockSignals(True)
            self.list.setCurrentRow(next(
                (i for i, t in enumerate(self._all) if t.name == self.working.name), 0))
            self.list.blockSignals(False)
            return
        theme = self._all[row]
        self.saved = theme
        self.window_.apply_theme(theme)
        self._load(theme)

    # -- editor ------------------------------------------------------------

    def _load(self, theme: Theme) -> None:
        self._loading = True
        self.working = replace(theme, colors=dict(theme.colors))
        self.dirty = False
        self.name.setText(theme.name)
        self.base.setCurrentIndex(0 if theme.base == "dark" else 1)
        for key in self.swatches:
            self._show_colour(key)
        self._loading = False
        self._update_state()

    def _show_colour(self, key: str) -> None:
        value = self.working.colors[key]
        self.swatches[key].setStyleSheet(
            f"QPushButton {{ background: {value}; border: 1px solid "
            f"{self.working.colors['border']}; border-radius: 6px; }}")
        self.hexes[key].setText(value)

    def _update_state(self) -> None:
        mine = not self.working.builtin
        self.save_btn.setEnabled(mine and self.dirty)
        self.save_btn.setToolTip(
            "Save your changes" if mine and self.dirty else
            "Nothing has changed yet" if mine else
            "Built-in themes stay as they are; use “Save as new…” to keep your changes")
        self.delete_btn.setEnabled(mine)
        self.name.setReadOnly(not mine)
        if self.working.builtin:
            self.note.setText("A built-in theme. Change anything you like; "
                              "“Save as new…” keeps it as your own.")
        else:
            self.note.setText(f"Your theme, stored in {self.working.path}. "
                              "Edit here, or in any text editor and re-open this window.")
        self.setWindowTitle("Themes" + ("  ·  unsaved changes" if self.dirty else ""))

    def _changed(self) -> None:
        self.dirty = True
        self.window_.apply_theme(self.working, remember=False)
        for key in self.swatches:  # the swatch borders use the theme's own line colour
            self._show_colour(key)
        self._update_state()

    def _pick_colour(self, key: str) -> None:
        label = next(lbl for k, lbl, _d in ROLES if k == key)
        chosen = QColorDialog.getColor(QColor(self.working.colors[key]), self, label)
        if chosen.isValid():
            self.working.colors[key] = chosen.name()
            self._changed()

    def _hex_edited(self, key: str) -> None:
        text = self.hexes[key].text().strip()
        if not text.startswith("#"):
            text = "#" + text
        if HEX.match(text):
            if text.lower() != self.working.colors[key]:
                self.working.colors[key] = text.lower()
                self._changed()
        else:
            self._show_colour(key)  # put the valid value back

    def _name_edited(self, text: str) -> None:
        if not self._loading and text.strip():
            self.working.name = text.strip()
            self.dirty = True
            self._update_state()

    def _base_edited(self, _index: int) -> None:
        if not self._loading:
            self.working.base = self.base.currentData()
            self.dirty = True
            self._update_state()

    # -- saving ------------------------------------------------------------

    def _save(self) -> bool:
        try:
            old_path = self.working.path
            saved = themes.save_user(self.working)
            if old_path and saved.path != old_path:
                old_path.unlink(missing_ok=True)
        except (ThemeError, OSError) as exc:
            QMessageBox.warning(self, "Not saved", str(exc))
            return False
        self._commit(saved)
        return True

    def _save_as(self) -> None:
        default = f"{self.working.name} (mine)" if self.working.builtin else f"{self.working.name} copy"
        name, ok = QInputDialog.getText(self, "Save as new theme", "Name:", text=default)
        if not ok or not name.strip():
            return
        candidate = replace(self.working, name=name.strip(), builtin=False, path=None,
                            colors=dict(self.working.colors))
        if any(t.name == candidate.name for t in themes.user_themes()):
            if QMessageBox.question(self, "Replace theme",
                                    f"You already have a theme called “{candidate.name}”. Replace it?") \
                    != QMessageBox.StandardButton.Yes:
                return
            candidate.path = next(t.path for t in themes.user_themes() if t.name == candidate.name)
        try:
            saved = themes.save_user(candidate)
        except (ThemeError, OSError) as exc:
            QMessageBox.warning(self, "Not saved", str(exc))
            return
        self._commit(saved)

    def _commit(self, saved: Theme) -> None:
        self.saved = saved
        self.dirty = False
        self.window_.apply_theme(saved)
        self._reload_list(select=saved.name)

    def _resolve_unsaved(self) -> bool:
        """Save, discard or keep editing. False means stay where we are."""
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved changes")
        box.setText(f"Keep your changes to “{self.working.name}”?")
        save = box.addButton("Save…" if self.working.builtin else "Save",
                             QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Keep editing", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is save:
            if self.working.builtin:
                self._save_as()
                return not self.dirty
            return self._save()
        if box.clickedButton() is discard:
            self.dirty = False
            self.window_.apply_theme(self.saved)
            return True
        return False

    def reject(self) -> None:
        """Every way out ends here: Close, Escape and the window's own close
        button (QDialog turns a close event into reject). So the unsaved-changes
        question is asked in one place, and nothing re-enters close()."""
        if self.dirty and not self._resolve_unsaved():
            return
        super().reject()

    # -- files -------------------------------------------------------------

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import a theme", str(Path.home()),
                                              "Theme files (*.json);;All files (*)")
        if not path:
            return
        try:
            theme, warnings = themes.import_file(Path(path))
        except ThemeError as exc:
            QMessageBox.warning(self, "Cannot import this theme", str(exc))
            return
        if warnings:
            QMessageBox.information(self, "Imported, with notes", "\n".join(warnings))
        self._commit(theme)

    def _export(self) -> None:
        suggested = str(Path.home() / f"{themes._slug(self.working.name)}.json")
        path, _ = QFileDialog.getSaveFileName(self, "Export theme", suggested,
                                              "Theme files (*.json)")
        if path:
            try:
                themes.export(self.working, Path(path))
            except OSError as exc:
                QMessageBox.warning(self, "Not exported", str(exc))

    def _open_folder(self) -> None:
        folder = themes.user_dir()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _delete(self) -> None:
        if self.working.builtin:
            return
        if QMessageBox.question(self, "Delete theme", f"Delete “{self.working.name}”?") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            themes.delete_user(self.working)
        except ThemeError as exc:
            QMessageBox.warning(self, "Not deleted", str(exc))
            return
        fallback = next(t for t in themes.builtin_themes() if t.base == self.working.base)
        self.dirty = False
        self._commit(fallback)
