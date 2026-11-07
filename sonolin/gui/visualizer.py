"""Movement in the now-playing card while music plays.

For a Library song, bars show the music itself: they come from the file
(`spectrum.analyse`), played back against the speaker's position. Anything
else (Spotify, radio) never sends its sound to this computer, so instead of
bars that would pretend to follow it, dots drift slowly upwards like bubbles:
plainly not a picture of the sound, and with no line to be mistaken for the
progress bar below. Bars settle flat on pause and the dots fade away; the timer
stops once they have, so an idle window costs nothing.
"""

from __future__ import annotations

import math
import random
import time

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QSizePolicy, QWidget

from ..spectrum import BANDS, FPS, Spectrum
from . import style

#: Tallest the bars grow, however tall the card is.
MAX_HEIGHT = 110
GAP = 3
#: The narrowest a bar is drawn; in a narrow window neighbouring bands share one.
MIN_BAR = 6
#: Narrower than this, nothing is drawn rather than a crowded smudge.
MIN_WIDTH = 120
#: How far a bar moves towards its target per frame: up quickly, down slowly,
#: which is what makes bars read as music rather than noise.
RISE, FALL = 0.55, 0.14
#: How quickly the dots fade in when music starts, and away when it stops.
FADE = 0.06


def _bubbles(count: int = 56) -> list[tuple[float, float, float, float, float]]:
    """(across, start height, radius, rise per second, sway phase) for each dot.

    Fixed, not random each run, so the picture is the same every time; spread
    evenly across the width, with the rest left to chance.
    """
    rng = random.Random(7)
    return [((i + rng.random()) / count, rng.random(), 1.5 + rng.random() * 2.5,
             0.04 + rng.random() * 0.07, rng.random() * math.tau) for i in range(count)]


BUBBLES = _bubbles()


class Visualizer(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(0)
        self.spectrum: Spectrum | None = None
        self.playing = False
        self._base, self._stamp = 0.0, time.monotonic()
        self.levels = [0.0] * BANDS
        self.shown = 0.0           # how visible the dots are, 0 to 1
        self._phase = 0.0          # the dots' clock; stands still while paused
        self._last = time.monotonic()
        self._timer = QTimer(self, interval=1000 // FPS)
        self._timer.timeout.connect(self.step)

    def set_spectrum(self, spectrum: Spectrum | None) -> None:
        """The measured song for the bars, or None for the drifting dots."""
        self.spectrum = spectrum
        self._wake()

    def sync(self, position: float, playing: bool) -> None:
        """Where the speaker is in the song. The bars keep their own clock in
        between, so they move every frame rather than once a second."""
        self._base, self._stamp, self.playing = position, time.monotonic(), playing
        self._wake()

    def _wake(self) -> None:
        if self.isVisible() and not self._timer.isActive() and \
                (self.playing or any(self.levels) or self.shown):
            self._last = time.monotonic()
            self._timer.start()

    def position(self) -> float:
        return self._base + (time.monotonic() - self._stamp if self.playing else 0.0)

    def step(self) -> None:
        now = time.monotonic()
        measured = self.spectrum is not None
        if self.playing and measured:
            targets = self.spectrum.at(self.position())
        else:
            targets = [0.0] * BANDS
        self.levels = [
            level + (target - level) * (RISE if target > level else FALL)
            for level, target in zip(self.levels, targets)
        ]
        self.shown += ((1.0 if self.playing and not measured else 0.0) - self.shown) * FADE
        if self.playing:
            self._phase += now - self._last
        self._last = now
        if not self.playing and max(self.levels) < 0.01 and self.shown < 0.01:
            self.levels, self.shown = [0.0] * BANDS, 0.0
            self._timer.stop()
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._wake()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()  # a minimised or tray-hidden window draws nothing

    def paintEvent(self, event) -> None:
        h = min(self.height(), MAX_HEIGHT)
        if h < 12 or self.width() < MIN_WIDTH:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        measured = self.spectrum is not None
        # Drawn while it is the current kind, or while it is still settling
        # after the song changed to the other kind.
        if measured or max(self.levels) > 0.01:
            self._paint_bars(painter, h, resting_dots=measured)
        if self.shown > 0.01:
            self._paint_bubbles(painter, h)
        painter.end()

    def _paint_bars(self, painter: QPainter, h: float, resting_dots: bool) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        colour = QColor(style.C["accent"])
        count = max(8, min(BANDS, int((self.width() + GAP) // (MIN_BAR + GAP))))
        # Each bar shows the loudest of the bands it stands for.
        levels = [max(self.levels[j * BANDS // count:(j + 1) * BANDS // count])
                  for j in range(count)]
        width = (self.width() - GAP * (count - 1)) / count
        bottom = self.height()
        for i, level in enumerate(levels):
            bar = max(2.0 if resting_dots else 0.0, level * h)  # at rest, a row of dots
            if bar <= 0:
                continue
            colour.setAlphaF(0.3 + 0.7 * level)
            painter.setBrush(colour)
            painter.drawRoundedRect(QRectF(i * (width + GAP), bottom - bar, width, bar), 1.5, 1.5)

    def _paint_bubbles(self, painter: QPainter, h: float) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        colour = QColor(style.C["accent"])
        w, bottom = self.width(), self.height()
        for across, start, radius, rise, sway in BUBBLES:
            height = (start + self._phase * rise) % 1.0   # 0 at the bottom, 1 at the top
            # Each dot fades in as it leaves the bottom and out as it nears the top.
            colour.setAlphaF(self.shown * (0.15 + 0.55 * math.sin(math.pi * height)))
            painter.setBrush(colour)
            x = across * w + 6 * math.sin(self._phase * 0.7 + sway)
            painter.drawEllipse(QPointF(x, bottom - radius - height * (h - 2 * radius)),
                                radius, radius)
