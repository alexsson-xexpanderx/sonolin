"""Spoken notifications.

Two halves: turn text into a WAV with a local speech synthesiser, then get the
speaker to play it. The second half matters more than the first — the naive way
is to take over the queue and put it back afterwards, which pauses the music and
loses the exact position. The websocket `audioClip` command layers the clip over
whatever is playing instead, ducking the music and restoring it with no gap, so
that is what `say` uses.

The synthesiser is `espeak-ng`, which is a small dependency and offline. Any
command that writes a WAV to a path would do; `ENGINES` is where to add one.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

#: name -> argv builder taking (text, out_path, voice, words_per_minute)
ENGINES: dict[str, callable] = {
    "espeak-ng": lambda text, out, voice, wpm: [
        "espeak-ng", "-w", str(out), "-v", voice or "en", "-s", str(wpm), text,
    ],
    "espeak": lambda text, out, voice, wpm: [
        "espeak", "-w", str(out), "-v", voice or "en", "-s", str(wpm), text,
    ],
}


def available_engine() -> str | None:
    for name in ENGINES:
        if shutil.which(name):
            return name
    return None


async def synthesise(
    text: str, *, voice: str = "en", wpm: int = 165, engine: str | None = None
) -> bytes:
    """Render `text` to WAV bytes."""
    name = engine or available_engine()
    if name is None:
        raise RuntimeError(
            "no speech synthesiser found — install espeak-ng for spoken notifications"
        )
    if not text.strip():
        raise ValueError("nothing to say")
    with tempfile.TemporaryDirectory(prefix="sonolin-tts-") as tmp:
        out = Path(tmp) / "clip.wav"
        cmd = ENGINES[name](text, out, voice, wpm)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(
                f"{name} failed: {err.decode('utf-8', 'replace').strip() or proc.returncode}"
            )
        return out.read_bytes()


def voices(engine: str | None = None) -> list[tuple[str, str]]:
    """Available (code, name) voices, for a UI to offer."""
    import subprocess

    name = engine or available_engine()
    if name is None:
        return []
    try:
        out = subprocess.run(
            [name, "--voices"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4:
            found.append((parts[1], parts[3]))
    return found
