"""The local music library: scan a folder tree, index it, group it.

This replaces noson-app's `NosonMediaScanner` plugin. Sonos players fetch media
over HTTP themselves and cannot read this machine's disk, so a scanned track is
only playable once `mediaserver` is running to serve it.

The index is held in memory and rebuilt on demand. A library of a few tens of
thousands of tracks costs a few tens of megabytes, which is the same bargain
noson-app makes.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import tags as tagreader
from .tags import AUDIO_SUFFIXES, Tags

log = logging.getLogger(__name__)


def default_music_dirs() -> list[Path]:
    """Where to look when the user has not said.

    Honours the XDG music directory if one is configured, because that is what
    noson-app documents as the place it scans.
    """
    found: list[Path] = []
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "user-dirs.dirs"
    try:
        for line in config.read_text().splitlines():
            if line.startswith("XDG_MUSIC_DIR="):
                raw = line.split("=", 1)[1].strip().strip('"')
                found.append(Path(os.path.expandvars(raw.replace("$HOME", str(Path.home())))))
    except OSError:
        pass
    found.append(Path.home() / "Music")
    return [p for p in dict.fromkeys(found) if p.is_dir()]


@dataclass
class Album:
    name: str
    artist: str
    year: str = ""
    tracks: list[Tags] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return (self.artist.lower(), self.name.lower())

    @property
    def duration(self) -> float:
        return sum(t.duration for t in self.tracks)

    def sorted_tracks(self) -> list[Tags]:
        """Disc then track number, the way noson-app orders an album."""
        return sorted(self.tracks, key=lambda t: (t.disc, t.track, t.display_title.lower()))


class Library:
    """An in-memory index of local audio files."""

    def __init__(self, roots: list[Path] | None = None) -> None:
        self.roots: list[Path] = roots if roots is not None else default_music_dirs()
        self.tracks: list[Tags] = []
        self._by_path: dict[str, Tags] = {}
        self._lock = threading.RLock()
        self.scanning = False

    # -- scanning ----------------------------------------------------------

    def walk(self) -> Iterator[Path]:
        for root in self.roots:
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    if Path(name).suffix.lower() in AUDIO_SUFFIXES:
                        path = Path(dirpath) / name
                        # os.walk's followlinks=False only excludes directory links.
                        if not path.is_symlink():
                            yield path

    def scan(self, progress: Callable[[int, Path], None] | None = None) -> int:
        """Rebuild the index. Returns the number of tracks found.

        Cover art is deliberately not read here: it would dominate the scan time
        and the memory, and the media server reads it on demand instead.
        """
        with self._lock:
            self.scanning = True
        found: list[Tags] = []
        try:
            for count, path in enumerate(self.walk(), 1):
                try:
                    found.append(tagreader.read(path))
                except Exception:
                    log.exception("could not read %s", path)
                    continue
                if progress and count % 25 == 0:
                    progress(count, path)
        finally:
            with self._lock:
                self.tracks = found
                self._by_path = {str(t.path): t for t in found}
                self.scanning = False
        log.info("scanned %d tracks from %s", len(found), ", ".join(map(str, self.roots)))
        return len(found)

    def add_files(self, paths: list[Path]) -> list[Tags]:
        """Index individual files outside the scanned folders.

        The media server only serves indexed files, so a one-off file handed to
        the CLI has to be added here before it can be played.
        """
        added = []
        with self._lock:
            tracks = list(self.tracks)
            for path in paths:
                path = Path(path).resolve()
                if str(path) in self._by_path or path.suffix.lower() not in AUDIO_SUFFIXES:
                    continue
                t = tagreader.read(path)
                tracks.append(t)
                self._by_path[str(path)] = t
                added.append(t)
            self.tracks = tracks  # a new list, so the media server re-indexes
        return added

    def get(self, path: str) -> Tags | None:
        with self._lock:
            return self._by_path.get(path)

    # -- views -------------------------------------------------------------

    def albums(self) -> list[Album]:
        grouped: dict[tuple[str, str], Album] = {}
        with self._lock:
            snapshot = list(self.tracks)
        for t in snapshot:
            name = t.album or "Unknown Album"
            artist = t.display_artist
            key = (artist.lower(), name.lower())
            album = grouped.get(key)
            if album is None:
                album = grouped[key] = Album(name=name, artist=artist, year=t.year)
            album.tracks.append(t)
            if t.year and not album.year:
                album.year = t.year
        return sorted(grouped.values(), key=lambda a: (a.artist.lower(), a.name.lower()))

    def _distinct(self, attr: str, fallback: str) -> list[str]:
        with self._lock:
            snapshot = list(self.tracks)
        seen: dict[str, str] = {}
        for t in snapshot:
            value = (getattr(t, attr) or "").strip() or fallback
            seen.setdefault(value.lower(), value)
        return sorted(seen.values(), key=str.lower)

    def artists(self) -> list[str]:
        return self._distinct("album_artist", "") or self._distinct("artist", "Unknown Artist")

    def genres(self) -> list[str]:
        return self._distinct("genre", "Unknown Genre")

    def composers(self) -> list[str]:
        return self._distinct("composer", "Unknown Composer")

    def by_artist(self, artist: str) -> list[Album]:
        wanted = artist.lower()
        return [a for a in self.albums() if a.artist.lower() == wanted]

    def filter(self, *, genre: str | None = None, composer: str | None = None) -> list[Tags]:
        with self._lock:
            snapshot = list(self.tracks)
        if genre:
            snapshot = [t for t in snapshot if (t.genre or "").lower() == genre.lower()]
        if composer:
            snapshot = [t for t in snapshot if (t.composer or "").lower() == composer.lower()]
        return snapshot

    def search(self, needle: str, limit: int = 300) -> list[Tags]:
        """Substring match across title, artist and album."""
        q = needle.strip().lower()
        if not q:
            return []
        with self._lock:
            snapshot = list(self.tracks)
        hits = [
            t for t in snapshot
            if q in t.display_title.lower()
            or q in (t.artist or "").lower()
            or q in (t.album or "").lower()
            or q in (t.album_artist or "").lower()
        ]
        return hits[:limit]

    def stats(self) -> dict[str, int | float]:
        with self._lock:
            snapshot = list(self.tracks)
        return {
            "tracks": len(snapshot),
            "albums": len({(t.display_artist.lower(), (t.album or "").lower()) for t in snapshot}),
            "artists": len({t.display_artist.lower() for t in snapshot}),
            "duration": sum(t.duration for t in snapshot),
        }
