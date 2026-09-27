"""The settings window: things used now and then, kept off the main window.

Finding speakers again, adding one by address, grouping, and the look. Every
button calls straight back into the window, which does the work as it always
has; this is only where the buttons live.
"""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)


def _heading(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("sectionTitle")
    return label


class SettingsDialog(QDialog):
    def __init__(self, window) -> None:
        super().__init__(window)
        self.window_ = window
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self.buttons: dict[str, QPushButton] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 8, 22, 16)
        root.setSpacing(6)

        root.addWidget(_heading("SPEAKERS"))
        self._row(root, "Search the network for speakers again (F5).",
                  "Find speakers", window._discover)
        self._row(root, "Add a speaker that discovery cannot see, such as one on another "
                        "subnet or VLAN.", "Add by address…", window._add_by_ip)

        sp = window.current
        usable = sp is not None and sp.awake
        name = sp.name if sp is not None else "the selected speaker"
        root.addWidget(_heading("GROUPING"))
        self._row(root, f"Pull every speaker into {name}’s group.", "Group all here",
                  lambda: window._call("party_mode", then=window._discover), enabled=usable)
        self._row(root, f"Take {name} out of its group.", "Leave group",
                  lambda: window._call("unjoin", then=window._discover), enabled=usable)
        tip = QLabel("Right-click a speaker for these and for stereo pairs.")
        tip.setObjectName("hint")
        root.addWidget(tip)

        root.addWidget(_heading("APPEARANCE"))
        mode = QHBoxLayout()
        mode.setSpacing(4)
        mode.addWidget(QLabel("Colour mode"), 1)
        self.modes = QButtonGroup(self)
        for base in ("dark", "light"):
            button = QPushButton(base.capitalize())
            button.setCheckable(True)
            button.setObjectName("segment")
            self.modes.addButton(button)
            self.buttons[base] = button
            mode.addWidget(button)
        self.modes.buttonClicked.connect(self._mode_clicked)
        self._show_mode()
        root.addLayout(mode)
        self._row(root, "Pick, edit, import or export a colour theme.", "Themes…",
                  self._themes)

        # One width for the whole column of buttons; the two halves of the
        # colour-mode switch share it.
        column = max(b.sizeHint().width() for key, b in self.buttons.items()
                     if key not in ("dark", "light"))
        for key, button in self.buttons.items():
            button.setFixedWidth(column // 2 - 2 if key in ("dark", "light") else column)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        root.addSpacing(10)
        root.addWidget(close)

    def _row(self, root: QVBoxLayout, text: str, label: str, slot: Callable[[], None],
             *, enabled: bool = True) -> None:
        row = QHBoxLayout()
        row.setSpacing(16)
        description = QLabel(text)
        description.setWordWrap(True)
        button = QPushButton(label)
        button.setEnabled(enabled)
        button.clicked.connect(lambda: slot())
        row.addWidget(description, 1)
        row.addWidget(button)
        self.buttons[label] = button
        root.addLayout(row)

    def _show_mode(self) -> None:
        self.buttons[self.window_.current_theme().base].setChecked(True)

    def _mode_clicked(self, button: QPushButton) -> None:
        wanted = "dark" if button is self.buttons["dark"] else "light"
        if self.window_.current_theme().base != wanted:
            self.window_._toggle_mode()
        self._show_mode()

    def _themes(self) -> None:
        self.window_._open_themes()
        self._show_mode()  # the chosen theme may be on the other side
