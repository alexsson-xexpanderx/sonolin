"""Measuring a song for the bars, from a real file."""

import pytest

pytest.importorskip("numpy")

from sonolin.spectrum import BANDS, FPS, HIGH_HZ, LOW_HZ, analyse  # noqa: E402


def test_a_tone_lights_the_band_it_is_in(music_dir):
    spectrum = analyse(music_dir / "a.flac")  # a three-second 440 Hz tone
    assert abs(len(spectrum.frames) / spectrum.fps - 3) < 0.2
    levels = spectrum.at(1.5)
    loudest = max(range(BANDS), key=levels.__getitem__)
    # Bands are spaced evenly in pitch between LOW_HZ and HIGH_HZ.
    low = LOW_HZ * (HIGH_HZ / LOW_HZ) ** (loudest / BANDS)
    high = LOW_HZ * (HIGH_HZ / LOW_HZ) ** ((loudest + 1) / BANDS)
    assert low <= 440 * 1.05 and high >= 440 * 0.95, (low, high)
    assert levels[loudest] > 0.8
    assert levels[-1] < 0.3  # nothing up at 11 kHz


def test_outside_the_song_is_silence(music_dir):
    spectrum = analyse(music_dir / "b.mp3")
    assert spectrum.at(-1) == [0.0] * BANDS
    assert spectrum.at(60) == [0.0] * BANDS
    assert spectrum.fps == FPS


def test_high_notes_get_their_share(tmp_path):
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is needed to generate test audio")
    # Pink noise has the overall balance of recorded music; its bars should be level.
    noise = tmp_path / "pink.flac"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "anoisesrc=color=pink:duration=4:sample_rate=44100:seed=7", str(noise)],
                   check=True)
    levels = analyse(noise).frames[15:-15].mean(axis=0) / 255
    assert max(levels) - min(levels) < 0.12, levels
