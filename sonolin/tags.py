"""Audio metadata and embedded cover art, read without external dependencies.

libnoson carries its own parsers for FLAC, ID3, MP4 and Ogg rather than pulling
in a tag library, and this does the same. Only the fields a music browser
actually shows are decoded; anything unrecognised is skipped rather than guessed
at, so a malformed file yields a sparse `Tags` instead of an exception.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .localfiles import open_regular

AUDIO_SUFFIXES = {".flac", ".mp3", ".m4a", ".mp4", ".ogg", ".oga", ".opus"}


@dataclass
class Picture:
    mime: str
    data: bytes


@dataclass
class Tags:
    path: Path
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    genre: str = ""
    composer: str = ""
    year: str = ""
    track: int = 0
    disc: int = 0
    duration: float = 0.0
    channels: int = 0
    sample_rate: int = 0
    picture: Picture | None = field(default=None, repr=False)

    @property
    def display_artist(self) -> str:
        return self.album_artist or self.artist or "Unknown Artist"

    @property
    def display_title(self) -> str:
        return self.title or self.path.stem


def _int(value: str) -> int:
    """Parse a track or disc number, tolerating the ``3/12`` form."""
    m = re.match(r"\s*(\d+)", value or "")
    return int(m.group(1)) if m else 0


# -- Vorbis comments (FLAC and Ogg share this) ----------------------------

_VORBIS_MAP = {
    "TITLE": "title",
    "ARTIST": "artist",
    "ALBUM": "album",
    "ALBUMARTIST": "album_artist",
    "ALBUM ARTIST": "album_artist",
    "GENRE": "genre",
    "COMPOSER": "composer",
    "DATE": "year",
    "TRACKNUMBER": "track",
    "DISCNUMBER": "disc",
}


def _apply_vorbis(tags: Tags, blob: bytes) -> None:
    try:
        pos = 0
        (vendor_len,) = struct.unpack_from("<I", blob, pos)
        pos += 4 + vendor_len
        (count,) = struct.unpack_from("<I", blob, pos)
        pos += 4
        for _ in range(count):
            (length,) = struct.unpack_from("<I", blob, pos)
            pos += 4
            field_bytes = blob[pos : pos + length]
            pos += length
            if b"=" not in field_bytes:
                continue
            key, _, value = field_bytes.partition(b"=")
            name = key.decode("ascii", "replace").upper()
            if name == "METADATA_BLOCK_PICTURE" and tags.picture is None:
                import base64

                try:
                    tags.picture = _parse_flac_picture(base64.b64decode(value))
                except (ValueError, struct.error):
                    pass
                continue
            attr = _VORBIS_MAP.get(name)
            if not attr:
                continue
            text = value.decode("utf-8", "replace")
            if attr in ("track", "disc"):
                setattr(tags, attr, _int(text))
            elif attr == "year":
                tags.year = text[:4]
            else:
                setattr(tags, attr, text)
    except struct.error:
        pass


# -- FLAC ------------------------------------------------------------------


def _read_flac(fh, tags: Tags) -> None:
    if fh.read(4) != b"fLaC":
        return
    while True:
        header = fh.read(4)
        if len(header) < 4:
            return
        last = bool(header[0] & 0x80)
        block_type = header[0] & 0x7F
        size = int.from_bytes(header[1:4], "big")
        body = fh.read(size)
        if block_type == 0 and len(body) >= 18:  # STREAMINFO
            bits = int.from_bytes(body[10:18], "big")
            tags.sample_rate = (bits >> 44) & 0xFFFFF
            tags.channels = ((bits >> 41) & 0x7) + 1
            total = bits & ((1 << 36) - 1)
            if tags.sample_rate:
                tags.duration = total / tags.sample_rate
        elif block_type == 4:  # VORBIS_COMMENT
            _apply_vorbis(tags, body)
        elif block_type == 6 and tags.picture is None:  # PICTURE
            tags.picture = _parse_flac_picture(body)
        if last:
            return


def _parse_flac_picture(body: bytes) -> Picture | None:
    try:
        pos = 4  # picture type
        (mime_len,) = struct.unpack_from(">I", body, pos)
        pos += 4
        mime = body[pos : pos + mime_len].decode("ascii", "replace")
        pos += mime_len
        (desc_len,) = struct.unpack_from(">I", body, pos)
        pos += 4 + desc_len
        pos += 16  # width, height, depth, colours
        (data_len,) = struct.unpack_from(">I", body, pos)
        pos += 4
        return Picture(mime or "image/jpeg", body[pos : pos + data_len])
    except struct.error:
        return None


# -- Ogg -------------------------------------------------------------------


def _ogg_duration(fh, tags: Tags) -> None:
    """Length is the granule position of the last page, in samples.

    Ogg carries no duration field; the final page's granule count is the sample
    index of the end of the stream, so it is the only cheap way to get it.
    """
    if not tags.sample_rate:
        return
    try:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        window = min(size, 65536)
        fh.seek(size - window)
        tail = fh.read(window)
        idx = tail.rfind(b"OggS")
        if idx < 0:
            return
        (granule,) = struct.unpack_from("<q", tail, idx + 6)
        if granule > 0:
            tags.duration = granule / tags.sample_rate
    except (OSError, struct.error):
        pass


def _read_ogg(fh, tags: Tags) -> None:
    """Walk Ogg pages far enough to find the comment header.

    Only the first few pages are examined: the identification header is first and
    the comment header follows it, so there is no reason to read the audio.
    """
    payload = b""
    for _ in range(24):
        header = fh.read(27)
        if len(header) < 27 or header[:4] != b"OggS":
            break
        seg_count = header[26]
        seg_table = fh.read(seg_count)
        body = fh.read(sum(seg_table))
        payload += body
        if b"\x03vorbis" in payload:
            _apply_vorbis(tags, payload.split(b"\x03vorbis", 1)[1])
            _ogg_duration(fh, tags)
            return
        if b"OpusTags" in payload:
            _apply_vorbis(tags, payload.split(b"OpusTags", 1)[1])
            _ogg_duration(fh, tags)
            return
        if b"\x01vorbis" in body:
            idx = body.index(b"\x01vorbis") + 7
            try:
                _, ch, rate = struct.unpack_from("<IBI", body, idx)
                tags.channels, tags.sample_rate = ch, rate
            except struct.error:
                pass
    _ogg_duration(fh, tags)


# -- ID3 / MP3 -------------------------------------------------------------

# Leave room for embedded artwork without allowing a header to allocate 256 MiB.
_MAX_ID3_SIZE = 16 * 1024 * 1024

_ID3_MAP = {
    "TIT2": "title", "TT2": "title",
    "TPE1": "artist", "TP1": "artist",
    "TALB": "album", "TAL": "album",
    "TPE2": "album_artist", "TP2": "album_artist",
    "TCON": "genre", "TCO": "genre",
    "TCOM": "composer", "TCM": "composer",
    "TRCK": "track", "TRK": "track",
    "TPOS": "disc", "TPA": "disc",
    "TDRC": "year", "TYER": "year", "TYE": "year",
}


def _decode_id3_text(raw: bytes) -> str:
    if not raw:
        return ""
    encoding, rest = raw[0], raw[1:]
    codec = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}.get(encoding, "latin-1")
    try:
        return rest.decode(codec, "replace").split("\x00", 1)[0].strip()
    except LookupError:
        return rest.decode("latin-1", "replace").split("\x00", 1)[0].strip()


def _read_id3(fh, tags: Tags) -> int:
    """Read an ID3v2 tag and return the offset where audio frames begin."""
    head = fh.read(10)
    if len(head) < 10 or head[:3] != b"ID3":
        return 0
    major = head[3]
    flags = head[5]
    size = int.from_bytes(head[6:10], "big")
    # Syncsafe: seven bits per byte.
    size = ((size >> 24) & 0x7F) << 21 | ((size >> 16) & 0x7F) << 14 \
        | ((size >> 8) & 0x7F) << 7 | (size & 0x7F)
    if size > _MAX_ID3_SIZE:
        return size + 10
    blob = fh.read(size)
    if flags & 0x40:  # extended header, skip it
        try:
            (ext,) = struct.unpack_from(">I", blob, 0)
            blob = blob[4 + ext :]
        except struct.error:
            return size + 10

    pos = 0
    id_len, size_len = (3, 3) if major == 2 else (4, 4)
    while pos + id_len + size_len <= len(blob):
        frame_id = blob[pos : pos + id_len].decode("ascii", "replace")
        if not frame_id.strip("\x00"):
            break
        pos += id_len
        if major == 2:
            frame_size = int.from_bytes(blob[pos : pos + 3], "big")
            pos += 3
        else:
            raw = int.from_bytes(blob[pos : pos + 4], "big")
            # v2.4 sizes are syncsafe; v2.3 are plain. Detect by plausibility.
            syncsafe = ((raw >> 24) & 0x7F) << 21 | ((raw >> 16) & 0x7F) << 14 \
                | ((raw >> 8) & 0x7F) << 7 | (raw & 0x7F)
            frame_size = syncsafe if major >= 4 else raw
            pos += 4 + 2  # size plus frame flags
        body = blob[pos : pos + frame_size]
        pos += frame_size
        attr = _ID3_MAP.get(frame_id)
        if attr:
            text = _decode_id3_text(body)
            if attr in ("track", "disc"):
                setattr(tags, attr, _int(text))
            elif attr == "year":
                tags.year = text[:4]
            else:
                setattr(tags, attr, text)
        elif frame_id in ("APIC", "PIC") and tags.picture is None:
            tags.picture = _parse_apic(body, major)
    return size + 10


def _parse_apic(body: bytes, major: int) -> Picture | None:
    try:
        if major == 2:
            mime = {"PNG": "image/png"}.get(body[1:4].decode("ascii", "replace"), "image/jpeg")
            rest = body[5:]
        else:
            mime, _, rest = body[1:].partition(b"\x00")
            mime = mime.decode("ascii", "replace") or "image/jpeg"
            rest = rest[1:]  # picture type
        # Description is nul-terminated in the frame's own encoding.
        if body[0] in (1, 2):
            idx = rest.find(b"\x00\x00")
            rest = rest[idx + 2 :] if idx >= 0 else rest
        else:
            _, _, rest = rest.partition(b"\x00")
        return Picture(mime, rest) if rest else None
    except (IndexError, ValueError):
        return None


_BITRATES_V1_L3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
_RATES_V1 = [44100, 48000, 32000, 0]


def _mp3_duration(fh, audio_start: int, file_size: int, tags: Tags) -> None:
    """Estimate duration from the first frame header.

    Exact length needs either a Xing/VBR header or a full frame walk. The first
    frame's bitrate is right for constant-bitrate files and close enough to sort
    and display VBR ones; a wrong guess costs a slightly off duration, nothing more.
    """
    fh.seek(audio_start)
    chunk = fh.read(8192)
    for i in range(len(chunk) - 4):
        if chunk[i] != 0xFF or (chunk[i + 1] & 0xE0) != 0xE0:
            continue
        b1, b2 = chunk[i + 1], chunk[i + 2]
        version_bits = (b1 >> 3) & 0x03
        bitrate = _BITRATES_V1_L3[(b2 >> 4) & 0x0F]
        rate = _RATES_V1[(b2 >> 2) & 0x03]
        if not bitrate or not rate:
            continue
        if version_bits == 0b10:  # MPEG-2
            rate //= 2
        elif version_bits == 0b00:  # MPEG-2.5
            rate //= 4
        tags.sample_rate = rate
        tags.channels = 1 if ((chunk[i + 3] >> 6) & 0x03) == 0b11 else 2
        if b"Xing" in chunk or b"Info" in chunk:
            marker = b"Xing" if b"Xing" in chunk else b"Info"
            j = chunk.index(marker)
            try:
                (flags,) = struct.unpack_from(">I", chunk, j + 4)
                if flags & 0x1:
                    (frames,) = struct.unpack_from(">I", chunk, j + 8)
                    tags.duration = frames * 1152 / rate
                    return
            except struct.error:
                pass
        tags.duration = (file_size - audio_start) * 8 / (bitrate * 1000)
        return


# -- MP4 / M4A -------------------------------------------------------------

_MP4_MAP = {
    b"\xa9nam": "title",
    b"\xa9ART": "artist",
    b"\xa9alb": "album",
    b"aART": "album_artist",
    b"\xa9gen": "genre",
    b"gnre": "genre",
    b"\xa9wrt": "composer",
    b"\xa9day": "year",
}


def _read_mp4(fh, tags: Tags, size: int) -> None:
    """Walk the atom tree to moov/udta/meta/ilst and moov/mvhd."""

    def walk(start: int, end: int, path: tuple[bytes, ...]) -> None:
        pos = start
        while pos + 8 <= end:
            fh.seek(pos)
            header = fh.read(8)
            if len(header) < 8:
                return
            atom_size = int.from_bytes(header[:4], "big")
            name = header[4:8]
            body_at = pos + 8
            if atom_size == 1:  # 64-bit extended size
                atom_size = int.from_bytes(fh.read(8), "big")
                body_at += 8
            if atom_size < 8:
                return
            stop = min(pos + atom_size, end)
            if name == b"mdhd" and not tags.sample_rate:
                # mdhd's timescale is the audio sample rate for a sound track.
                fh.seek(body_at)
                data = fh.read(min(stop - body_at, 24))
                try:
                    tags.sample_rate = struct.unpack_from(
                        ">I", data, 20 if data[0] == 1 else 12
                    )[0]
                except struct.error:
                    pass
            elif name in (b"moov", b"udta", b"trak", b"mdia", b"minf", b"stbl"):
                walk(body_at, stop, path + (name,))
            elif name == b"meta":
                walk(body_at + 4, stop, path + (name,))  # meta has a version word
            elif name == b"ilst":
                read_ilst(body_at, stop)
            elif name == b"mvhd":
                fh.seek(body_at)
                data = fh.read(min(stop - body_at, 32))
                try:
                    if data[0] == 1:
                        timescale, duration = struct.unpack_from(">IQ", data, 20)
                    else:
                        timescale, duration = struct.unpack_from(">II", data, 12)
                    if timescale:
                        tags.duration = duration / timescale
                except struct.error:
                    pass
            pos += atom_size

    def read_ilst(start: int, end: int) -> None:
        pos = start
        while pos + 8 <= end:
            fh.seek(pos)
            header = fh.read(8)
            if len(header) < 8:
                return
            item_size = int.from_bytes(header[:4], "big")
            key = header[4:8]
            if item_size < 8:
                return
            fh.seek(pos + 8)
            payload = fh.read(item_size - 8)
            value = payload[16:] if len(payload) > 16 else b""
            attr = _MP4_MAP.get(key)
            if attr:
                text = value.decode("utf-8", "replace").strip("\x00")
                if attr == "year":
                    tags.year = text[:4]
                else:
                    setattr(tags, attr, text)
            elif key == b"trkn" and len(value) >= 4:
                tags.track = int.from_bytes(value[2:4], "big")
            elif key == b"disk" and len(value) >= 4:
                tags.disc = int.from_bytes(value[2:4], "big")
            elif key == b"covr" and tags.picture is None and value:
                mime = "image/png" if value[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
                tags.picture = Picture(mime, value)
            pos += item_size

    walk(0, size, ())


# -- entry point -----------------------------------------------------------


def read(path: str | os.PathLike, *, want_picture: bool = False) -> Tags:
    """Read what metadata `path` carries.

    `want_picture` is off by default because cover art dominates both the read
    time and the memory a library scan uses; the art is fetched on demand by the
    media server instead.
    """
    p = Path(path)
    tags = Tags(path=p)
    suffix = p.suffix.lower()
    try:
        with open_regular(p) as fh:
            size = os.fstat(fh.fileno()).st_size
            if suffix == ".flac":
                _read_flac(fh, tags)
            elif suffix in (".ogg", ".oga", ".opus"):
                _read_ogg(fh, tags)
            elif suffix == ".mp3":
                audio_start = _read_id3(fh, tags)
                _mp3_duration(fh, audio_start, size, tags)
            elif suffix in (".m4a", ".mp4"):
                _read_mp4(fh, tags, size)
    except OSError:
        return tags
    if not want_picture:
        tags.picture = None
    return tags


def read_picture(path: str | os.PathLike) -> Picture | None:
    """Fetch only the embedded cover art, or a cover file sitting beside it."""
    tags = read(path, want_picture=True)
    if tags.picture:
        return tags.picture

    def open_cover(path, flags):
        # Reject symlinks at open time, including ones swapped in during lookup.
        # Nonblocking lets us reject FIFOs without waiting for a writer.
        return os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)

    folder = Path(path).parent
    for name in ("cover", "folder", "front", "album", "albumart"):
        for ext, mime in ((".jpg", "image/jpeg"), (".jpeg", "image/jpeg"), (".png", "image/png")):
            candidate = folder / f"{name}{ext}"
            try:
                with open_regular(candidate) as fh:
                    return Picture(mime, fh.read())
            except OSError:
                continue
    return None
