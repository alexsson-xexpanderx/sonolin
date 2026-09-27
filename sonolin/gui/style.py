"""The look: themed colours on Fusion, so it renders the same everywhere.

Fusion is Qt's own style and ignores the desktop theme, which is the point: a
native style would inherit whatever the platform does and fight the sheet
below. Colours live in one table so the palette and the sheet cannot drift.
"""

from __future__ import annotations

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

from .themes import DARK

#: The colours in force. Mutated in place by `apply`, so code that reads
#: ``style.C[...]`` at paint time follows a theme change without being told.
C: dict[str, str] = dict(DARK)


def sheet() -> str:
    """The stylesheet for the colours currently in `C`."""
    return f"""
* {{
    font-size: 10.5pt;
}}
QMainWindow, QDialog {{
    background: {C['bg']};
}}
QWidget {{
    color: {C['text']};
}}
QToolBar {{
    background: {C['bg']};
    border: none;
    border-bottom: 1px solid {C['border']};
    padding: 6px 8px;
    spacing: 6px;
}}
QToolBar QToolButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 6px 12px;
    color: {C['muted']};
}}
QToolBar QToolButton:hover {{
    background: {C['raised']};
    border-color: {C['border']};
    color: {C['text']};
}}
QStatusBar {{
    background: {C['bg']};
    color: {C['muted']};
    border-top: 1px solid {C['border']};
}}
QSplitter::handle {{
    background: {C['bg']};
}}

/* ---- side bar ------------------------------------------------------- */
#sidebar {{
    background: {C['surface']};
    border-right: 1px solid {C['border']};
}}
#sidebarTitle {{
    color: {C['faint']};
    font-size: 8.5pt;
    font-weight: 700;
    letter-spacing: 1.5px;
    padding: 14px 16px 6px 16px;
}}
#speakerList, #navList {{
    background: transparent;
    border: none;
    outline: none;
    padding: 4px 8px;
}}
#sidebarButton {{
    background: transparent;
    border: none;
    border-radius: 9px;
    color: {C['muted']};
    padding: 8px 12px;
    text-align: left;
}}
#sidebarButton:hover {{
    background: {C['raised']};
    color: {C['text']};
}}
#sectionTitle {{
    color: {C['faint']};
    font-size: 8.5pt;
    font-weight: 700;
    letter-spacing: 1.5px;
    padding-top: 14px;
}}
QPushButton#segment:checked {{
    background: {C['accent_lo']};
    border-color: {C['accent']};
    color: {C['selection_text']};
}}
#navList {{
    padding-top: 6px;
    border-bottom: 1px solid {C['border']};
    border-radius: 0;
}}
/* ---- now playing ---------------------------------------------------- */
#nowPlaying {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 {C['raised']}, stop:1 {C['surface']});
    border: 1px solid {C['border']};
    border-radius: 16px;
}}
#coverArt {{
    background: {C['bg']};
    border: 1px solid {C['border']};
    border-radius: 12px;
    color: {C['faint']};
}}
#trackTitle[compact="true"] {{
    font-size: 12.5pt;
}}
#trackTitle {{
    font-size: 19pt;
    font-weight: 700;
    color: {C['text']};
}}
#trackArtist {{
    font-size: 12.5pt;
    color: {C['text']};
}}
#trackAlbum {{
    font-size: 10.5pt;
    color: {C['muted']};
}}
#trackState {{
    font-size: 9pt;
    font-weight: 700;
    letter-spacing: 1px;
    color: {C['accent']};
}}
#timeLabel {{
    color: {C['muted']};
    font-size: 9pt;
    min-width: 42px;
}}

/* ---- transport ------------------------------------------------------ */
#transport QPushButton {{
    background: {C['raised']};
    border: 1px solid {C['border']};
    border-radius: 20px;
    min-width: 40px; max-width: 40px;
    min-height: 40px; max-height: 40px;
    font-size: 12pt;
    padding: 0;
}}
#transport QPushButton:hover {{
    background: {C['hover']};
    border-color: {C['accent_lo']};
}}
#transport QPushButton:checked {{
    color: {C['accent']};
    border-color: {C['accent']};
}}
#transport QPushButton#playButton {{
    background: {C['accent']};
    border: none;
    border-radius: 26px;
    min-width: 52px; max-width: 52px;
    min-height: 52px; max-height: 52px;
    font-size: 15pt;
    color: {C['on_accent']};
}}
#transport QPushButton#playButton:hover {{
    background: {C['accent_hi']};
}}

/* ---- general controls ----------------------------------------------- */
QPushButton {{
    background: {C['raised']};
    border: 1px solid {C['border']};
    border-radius: 8px;
    padding: 7px 14px;
}}
QPushButton:hover {{
    background: {C['hover']};
    border-color: {C['accent_lo']};
}}
QPushButton:pressed {{
    background: {C['accent_lo']};
}}
QPushButton:disabled {{
    color: {C['faint']};
    background: {C['surface']};
    border-color: {C['border']};
}}
QPushButton#accentButton:disabled {{
    color: {C['faint']};
    background: {C['surface']};
    border-color: {C['border']};
}}
QPushButton#danger {{
    color: {C['danger']};
}}
QPushButton#danger:hover {{
    border-color: {C['danger']};
}}
QLineEdit, QTextEdit, QSpinBox, QTimeEdit, QComboBox {{
    background: {C['bg']};
    border: 1px solid {C['border']};
    border-radius: 8px;
    padding: 6px 8px;
    selection-background-color: {C['accent_lo']};
}}
QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QTimeEdit:focus, QComboBox:focus {{
    border-color: {C['accent']};
}}
QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox QAbstractItemView {{
    background: {C['raised']};
    border: 1px solid {C['border']};
    selection-background-color: {C['accent_lo']};
}}
QCheckBox {{
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 18px; height: 18px;
    border-radius: 5px;
    border: 1px solid {C['border']};
    background: {C['bg']};
}}
QCheckBox::indicator:checked {{
    background: {C['accent']};
    border-color: {C['accent']};
}}
QSlider::groove:horizontal {{
    height: 4px;
    background: {C['border']};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {C['accent']};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {C['text']};
    width: 14px; height: 14px;
    margin: -5px 0;
    border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{
    background: {C['accent']};
}}

/* ---- pages --------------------------------------------------------- */
#pagePane {{
    background: {C['surface']};
    border: 1px solid {C['border']};
    border-radius: 14px;
}}

/* ---- lists and tables ----------------------------------------------- */
QListWidget, QTableWidget {{
    background: {C['bg']};
    border: 1px solid {C['border']};
    border-radius: 10px;
    alternate-background-color: {C['surface']};
    gridline-color: transparent;
    outline: none;
}}
QListWidget::item {{
    padding: 6px 8px;
    border-radius: 6px;
}}
QListWidget::item:selected, QTableWidget::item:selected {{
    background: {C['accent_lo']};
    color: {C['selection_text']};
}}
QListWidget::item:hover {{
    background: {C['raised']};
}}
QHeaderView::section {{
    background: {C['surface']};
    color: {C['faint']};
    border: none;
    border-bottom: 1px solid {C['border']};
    padding: 6px 8px;
    font-size: 8.5pt;
    font-weight: 700;
}}
QTableWidget::item {{
    padding: 4px 6px;
    border: none;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {C['border']};
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {C['faint']};
}}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    background: none;
    height: 0;
}}
QProgressBar {{
    background: {C['raised']};
    border: none;
    border-radius: 3px;
    max-height: 6px;
}}
QProgressBar::chunk {{
    background: {C['accent']};
    border-radius: 3px;
}}
QMenu {{
    background: {C['raised']};
    border: 1px solid {C['border']};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 18px;
    border-radius: 6px;
}}
QMenu::item:selected {{
    background: {C['accent_lo']};
}}
QMenu::separator {{
    height: 1px;
    background: {C['border']};
    margin: 4px 8px;
}}
/* ---- music-service browser ------------------------------------------ */
QLineEdit#browserSearch {{
    border-radius: 18px;
    padding: 8px 16px;
    background: {C['raised']};
    font-size: 10.5pt;
}}
QPushButton#navButton {{
    border-radius: 17px;
    min-width: 34px; max-width: 34px;
    min-height: 34px; max-height: 34px;
    padding: 0;
    font-size: 13pt;
}}
QPushButton#chip {{
    border-radius: 13px;
    min-height: 18px;
    padding: 4px 15px;
    background: {C['raised']};
    color: {C['muted']};
}}
QPushButton#chip:checked {{
    background: {C['text']};
    color: {C['bg']};
    border-color: {C['text']};
}}
QPushButton#accentButton {{
    background: {C['accent']};
    color: {C['on_accent']};
    border: 1px solid {C['accent']};
    border-radius: 17px;
    min-height: 18px;
    padding: 7px 22px;
    font-weight: 700;
}}
QPushButton#accentButton:hover {{
    background: {C['accent_hi']};
}}
QPushButton#pillButton {{
    border-radius: 17px;
    min-height: 18px;
    padding: 7px 18px;
}}
QPushButton#linkButton {{
    background: transparent;
    border: none;
    color: {C['muted']};
    font-weight: 700;
    padding: 2px 4px;
}}
QPushButton#linkButton:hover {{
    color: {C['text']};
}}
#crumbs {{
    font-size: 9.5pt;
    padding: 0 4px;
}}
QScrollArea#browserScroll, QWidget#browserPage {{
    background: transparent;
}}
QListView#browserView {{
    background: transparent;
    border: none;
}}
#hero {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 {C['accent_lo']}, stop:0.55 {C['raised']}, stop:1 {C['surface']});
    border-radius: 18px;
}}
#heroKind {{
    color: {C['muted']};
    font-size: 8.5pt;
    font-weight: 700;
    letter-spacing: 2px;
}}
#heroTitle {{
    font-size: 24pt;
    font-weight: 800;
}}
#heroSub {{
    color: {C['muted']};
    font-size: 10.5pt;
}}
#sectionHeader {{
    font-size: 13.5pt;
    font-weight: 800;
    padding-left: 4px;
}}
#emptyTitle {{
    font-size: 14pt;
    color: {C['muted']};
}}
#sectionTitle {{
    color: {C['faint']};
    font-size: 8.5pt;
    font-weight: 700;
    letter-spacing: 1px;
}}
#hint {{
    color: {C['muted']};
    font-size: 9.5pt;
}}
"""


def apply(app: QApplication, colors: dict[str, str] | None = None) -> None:
    """Style the whole application with `colors` (the current ones if None)."""
    if colors is not None:
        C.clear()
        C.update(colors)
    app.setStyle("Fusion")
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: C["bg"],
        QPalette.ColorRole.WindowText: C["text"],
        QPalette.ColorRole.Base: C["bg"],
        QPalette.ColorRole.AlternateBase: C["surface"],
        QPalette.ColorRole.Text: C["text"],
        QPalette.ColorRole.Button: C["raised"],
        QPalette.ColorRole.ButtonText: C["text"],
        QPalette.ColorRole.Highlight: C["accent_lo"],
        QPalette.ColorRole.HighlightedText: C["selection_text"],
        QPalette.ColorRole.ToolTipBase: C["raised"],
        QPalette.ColorRole.ToolTipText: C["text"],
        QPalette.ColorRole.PlaceholderText: C["faint"],
        QPalette.ColorRole.Link: C["accent"],
    }
    for role, colour in roles.items():
        pal.setColor(role, QColor(colour))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(C["faint"]))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(C["faint"]))
    app.setPalette(pal)
    app.setStyleSheet(sheet())
