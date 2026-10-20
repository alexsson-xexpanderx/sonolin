"""Finding the song behind a web link.

A share link (Suno's, for one) opens a web page, not a sound file, and a speaker
can only play the file. Pages that share music name the file for link previews
(`og:audio`) or for their own player; this reads the page and finds it. The
speaker then fetches the file itself, so nothing is downloaded to this machine
beyond the page.
"""

from __future__ import annotations

import html
import re
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlparse

#: A page larger than this is not read further; the preview tags are near the top.
MAX_PAGE = 4 * 1024 * 1024
TIMEOUT = 15

MIME = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".aac": "audio/aac",
        ".flac": "audio/flac", ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
        ".wav": "audio/wav", ".aif": "audio/aiff", ".aiff": "audio/aiff"}

#: Preview tags that name a playable file, best first.
AUDIO_TAGS = ("og:audio:secure_url", "og:audio:url", "og:audio", "twitter:player:stream")

# Suno leaves `og:audio` out and names the file in the page's own data, as
# escaped JSON: \"audio_url\":\"https://cdn1.suno.ai/<id>.mp3\". For a private
# song it names ".../api/forbidden" instead. Its other addresses ("media_urls")
# are scrambled files only Suno's own player can read; a speaker given one
# stops at once, so they are not used.
_AUDIO_URL = re.compile(r'\\?"audio_url\\?"\s*:\s*\\?"(https?://[^"\\]+)')

PRIVATE = ("This song is private on Suno, so Suno does not share its sound. Make it "
           "public on Suno, or download it from Suno and add it to your Library.")


@dataclass
class Link:
    url: str            #: the sound file the speaker fetches
    title: str
    art: str = ""
    mime: str = "audio/mpeg"


class _Meta(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: dict[str, str] = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs) -> None:
        a = dict(attrs)
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content") and key not in self.tags:
                self.tags[key] = a["content"]
        self._in_title = tag == "title"

    def handle_data(self, data) -> None:
        if self._in_title and not self.title:
            self.title = data.strip()

    def handle_endtag(self, tag) -> None:
        self._in_title = False


def _is_web(url: str) -> bool:
    return urlparse(url).scheme in ("http", "https")


def _audio_suffix(url: str) -> str:
    suffix = PurePosixPath(urlparse(url).path).suffix.lower()
    return suffix if suffix in MIME else ""


def _name(url: str) -> str:
    return unquote(PurePosixPath(urlparse(url).path).stem) or url


def _looks_like_audio(head: bytes) -> bool:
    """Whether a file starts the way a format the speaker plays does."""
    return (head[:3] == b"ID3" or head[4:8] == b"ftyp"
            or head[:4] in (b"fLaC", b"OggS", b"RIFF", b"FORM")
            or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0))  # MPEG frame


def _head(url: str) -> bytes:
    """The first bytes of a file, to tell real audio from a page or a scrambled file."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Sonolin)",
                                               "Range": "bytes=0-15"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read(16)


def _fetch(url: str) -> tuple[str, str, bytes]:
    """The final address after redirects, its content type, and the page."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Sonolin)"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        ctype = resp.headers.get("Content-Type", "")
        # A sound file needs no reading: the speaker fetches it.
        body = b"" if ctype.startswith("audio/") else resp.read(MAX_PAGE)
        return resp.geturl(), ctype, body


def resolve(address: str, fetch=_fetch, head=_head) -> Link:
    """The sound file behind `address`. Raises ValueError with a plain message.

    The file is checked before the speaker is given it: a speaker handed
    something it cannot decode just stops, and says nothing about why.
    """
    link = _find(address.strip(), fetch)
    try:
        start = head(link.url)
    except OSError as exc:
        raise ValueError(f"Could not open the song's file ({exc}).") from None
    if not _looks_like_audio(start):
        raise ValueError("That link does not lead to a sound file the speaker can play. "
                         "The site may protect its audio.")
    return link


def _find(address: str, fetch) -> Link:
    if not _is_web(address):
        raise ValueError("That is not a web link. It should start with https://")
    if suffix := _audio_suffix(address):
        return Link(address, _name(address), mime=MIME[suffix])
    try:
        final, ctype, body = fetch(address)
    except OSError as exc:
        raise ValueError(f"Could not open that link ({exc}).") from None
    if ctype.startswith("audio/"):
        return Link(final, _name(final), mime=ctype.split(";")[0].strip())

    charset = re.search(r"charset=([\w-]+)", ctype)
    try:
        page = body.decode(charset.group(1) if charset else "utf-8", errors="replace")
    except LookupError:  # a charset Python does not know
        page = body.decode("utf-8", errors="replace")
    meta = _Meta()
    meta.feed(page)
    audio = next((meta.tags[k] for k in AUDIO_TAGS if k in meta.tags), "")
    if not audio:
        found = _AUDIO_URL.search(page)
        if found and not _audio_suffix(found.group(1)):
            raise ValueError(PRIVATE)
        audio = found.group(1) if found else ""
    audio = urljoin(final, html.unescape(audio)) if audio else ""
    if not _is_web(audio):
        raise ValueError("No song found at that link. "
                         "It may be private, or the site may not share its audio.")
    title = meta.tags.get("og:title") or meta.tags.get("twitter:title") or meta.title
    art = meta.tags.get("og:image") or meta.tags.get("twitter:image") or ""
    suffix = _audio_suffix(audio)
    return Link(audio, title or _name(audio),
                art=urljoin(final, art) if art else "",
                mime=MIME.get(suffix, (meta.tags.get("og:audio:type") or "audio/mpeg")))
