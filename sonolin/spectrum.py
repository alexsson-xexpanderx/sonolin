"""What a song looks like as moving bars, worked out ahead of time.

The speaker plays the file and never sends the sound back, so the bars for a
Library song come from the file itself: decoded once, cut into short slices,
and each slice's loudness measured in `BANDS` bands from low notes to high.
Played back against the speaker's position, they move with what is heard.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

RATE = 22050          #: decoded sample rate; the bars stop at about 11 kHz anyway
FPS = 30              #: slices per second, as often as the bars are redrawn
HOP = RATE // FPS
WINDOW = 2048         #: samples per slice; enough for ~11 Hz steps in the bass
BANDS = 48
LOW_HZ, HIGH_HZ = 40.0, 11000.0
RANGE_DB = 50.0       #: how far below the song's loud parts a bar reads as empty
#: Recorded music gets quieter by a few dB with every octave up, so measured
#: as it is the high bars would hardly move. This much is added back per octave.
TILT_DB = 3.0
CHUNK = 512           #: slices measured at once, to keep memory flat on long songs


@dataclass
class Spectrum:
    fps: int
    frames: "object"   #: numpy uint8 array, one row of `BANDS` levels per slice

    def at(self, seconds: float) -> list[float]:
        """Band levels (0–1) at a point in the song; silence outside it."""
        i = int(seconds * self.fps)
        if not 0 <= i < len(self.frames):
            return [0.0] * BANDS
        return [v / 255 for v in self.frames[i]]


def _band_edges(np):
    """FFT bin boundaries, spaced evenly in pitch rather than in Hz.

    Each band gets at least one bin of its own: in the bass, bands are
    narrower than a bin and would otherwise repeat one another.
    """
    hz = np.geomspace(LOW_HZ, HIGH_HZ, BANDS + 1)
    bins = np.round(hz * WINDOW / RATE).astype(int)
    for i in range(1, len(bins)):
        bins[i] = max(bins[i], bins[i - 1] + 1)
    return bins


def analyse(path: str | Path, ffmpeg: str = "ffmpeg") -> Spectrum:
    """Decode `path` and measure it. Takes about a second for a song; run it
    off the GUI thread. Raises ImportError without numpy, OSError without ffmpeg."""
    import numpy as np

    pcm = subprocess.run(
        [ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-vn",
         "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
        capture_output=True, check=True, timeout=300,
    ).stdout
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    if len(samples) < WINDOW:
        return Spectrum(FPS, np.zeros((0, BANDS), dtype=np.uint8))

    slices = np.lib.stride_tricks.sliding_window_view(samples, WINDOW)[::HOP]
    window = np.hanning(WINDOW).astype(np.float32)
    edges = _band_edges(np)
    db = np.empty((len(slices), BANDS), dtype=np.float32)
    for start in range(0, len(slices), CHUNK):
        mags = np.abs(np.fft.rfft(slices[start:start + CHUNK] * window, axis=1))
        bands = np.add.reduceat(mags, edges[:-1], axis=1) / np.diff(edges)
        db[start:start + CHUNK] = 20 * np.log10(bands + 1e-9)
    db += TILT_DB * np.log2(np.geomspace(LOW_HZ, HIGH_HZ, BANDS) / LOW_HZ)

    # Measured against the song's own loud parts, so a quiet recording still
    # fills the bars; the very loudest moments may touch the top.
    top = np.percentile(db, 99.5)
    levels = np.clip((db - (top - RANGE_DB)) / RANGE_DB, 0, 1)
    return Spectrum(FPS, (levels * 255).astype(np.uint8))
