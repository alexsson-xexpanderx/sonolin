"""An HTTP server the speakers fetch from.

Sonos players pull media themselves over HTTP; they cannot read this machine's
disk and they will not accept a `file://` URI. So anything local — a track, its
cover art, a spoken notification, this machine's own audio output — has to be
published on a real URL bound to a LAN address the speaker can route to.

This is the equivalent of libnoson's `RequestBroker` and its three handlers
(`FileStreamer`, `ImageService`, `PulseStreamer`), which is the one part of
noson-app that has no counterpart anywhere in the Python Sonos ecosystem.

**Only files present in the library index are reachable.** Paths never appear in
a URL; each track is addressed by a digest of its path, and a digest that is not
in the index is a 404. There is no route that takes a filesystem path, so there
is nothing to traverse. Note all the same that while this is running, anything on
the local network can fetch the indexed audio and art without authenticating —
the speakers offer no way to present a credential.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import secrets
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

from . import tags as tagreader
from .library import Library

log = logging.getLogger(__name__)

CONTENT_TYPES = {
    ".flac": "audio/flac",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
}

#: How the live capture is encoded. FLAC is what libnoson streams and is
#: lossless; MP3 is the compatibility fallback for older players.
STREAM_FORMATS = {
    "flac": (["-f", "flac", "-compression_level", "0"], "audio/flac"),
    "mp3": (["-f", "mp3", "-b:a", "320k"], "audio/mpeg"),
    "wav": (["-f", "wav"], "audio/wav"),
}


def token_for(path: str | Path) -> str:
    """A stable, opaque id for a file.

    Derived from the path so that a URI already sitting in a Sonos queue still
    resolves after this process restarts.
    """
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:20]


def local_ip(peer: str = "192.168.1.1") -> str:
    """The address on the interface that routes towards the speakers.

    Binding to 0.0.0.0 would work but the URL handed to the speaker has to name
    a concrete address, and on a multi-homed machine the wrong one is silently
    unreachable. Opening a UDP socket establishes the route without sending
    anything.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((peer, 9))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


@dataclass
class Clip:
    """A short generated file, such as a spoken notification."""

    data: bytes
    content_type: str
    created: float
    fetched: asyncio.Event = field(default_factory=asyncio.Event)


class MediaServer:
    """Publishes library tracks, cover art, clips and a live capture."""

    def __init__(
        self,
        library: Library | None = None,
        *,
        host: str | None = None,
        port: int = 0,
        peer: str = "192.168.1.1",
        capture_source: str | None = None,
    ) -> None:
        self.library = library or Library([])
        self.host = host or local_ip(peer)
        self.requested_port = port
        self.port: int | None = None
        self.capture_source = capture_source
        self._clips: dict[str, Clip] = {}
        self._runner: web.AppRunner | None = None
        self._art_cache: dict[str, tuple[bytes, str]] = {}
        self._tokens: dict[str, Path] = {}
        self._token_source: object = None
        self._live_token: str | None = None
        self._live_tasks: set[asyncio.Task] = set()

    # -- lifecycle ---------------------------------------------------------

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.add_routes([
            web.get("/", self._index),
            web.get("/music/{token}", self._music),
            web.get("/art/{token}", self._art),
            web.get("/clip/{name}", self._clip),
            web.get("/stream/live.{fmt}", self._live),
        ])
        return app

    async def start(self) -> str:
        """Start listening. Returns the base URL the speakers should use."""
        if self._runner is not None:
            return self.base_url
        self._runner = web.AppRunner(self._build_app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.requested_port)
        try:
            await site.start()
        except OSError as exc:
            if self.requested_port == 0:
                raise
            # Taken, most likely by a second copy of this app. Any free port
            # works for the speakers; only a firewall rule would miss it.
            log.warning("port %d unavailable (%s); using a random one",
                        self.requested_port, exc.strerror)
            site = web.TCPSite(self._runner, self.host, 0)
            await site.start()
        sock = next(iter(site._server.sockets))  # the bound socket, for port 0
        self.port = sock.getsockname()[1]
        log.info("media server on %s", self.base_url)
        return self.base_url

    async def stop(self) -> None:
        await self.stop_stream()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self.port = None

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise RuntimeError("media server is not running")
        return f"http://{self.host}:{self.port}"

    # -- URL construction --------------------------------------------------

    def music_url(self, path: str | Path) -> str:
        # The suffix is not decoration: players judge whether they can play an
        # HTTP resource partly by its extension, and refuse to queue one without.
        return f"{self.base_url}/music/{token_for(path)}{Path(path).suffix.lower()}"

    def art_url(self, path: str | Path) -> str:
        return f"{self.base_url}/art/{token_for(path)}"

    async def start_stream(self, fmt: str = "flac") -> str:
        """Authorize a desktop session until explicitly stopped or replaced.

        Run on the server's loop, like stop_stream. The URL is a bearer
        capability: only the selected player should be given it.
        """
        if fmt not in STREAM_FORMATS:
            raise ValueError(f"unknown stream format {fmt!r}")
        base = self.base_url
        await self.stop_stream()
        self._live_token = secrets.token_urlsafe(32)
        return f"{base}/stream/live.{fmt}?token={self._live_token}"

    async def stop_stream(self) -> None:
        """Revoke the URL and stop every capture, including stalled listeners."""
        self._live_token = None
        tasks = list(self._live_tasks)
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def add_clip(self, data: bytes, content_type: str = "audio/wav", suffix: str = "wav") -> str:
        """Publish a generated clip and return its URL.

        Clips are held in memory and pruned after ten minutes; they exist only
        long enough for the speaker to fetch them once.
        """
        name = f"{hashlib.sha1(data[:4096] + str(len(data)).encode()).hexdigest()[:16]}.{suffix}"
        self._clips[name] = Clip(data, content_type, time.time())
        cutoff = time.time() - 600
        for key in [k for k, v in self._clips.items() if v.created < cutoff]:
            self._clips.pop(key, None)
        return f"{self.base_url}/clip/{name}"

    # -- handlers ----------------------------------------------------------

    async def _index(self, request: web.Request) -> web.Response:
        stats = self.library.stats()
        return web.json_response({
            "service": "Sonolin media server",
            "tracks": stats["tracks"],
            "clips": len(self._clips),
            "capture_source": self.capture_source or "(default monitor)",
        })

    def _resolve(self, token: str) -> Path | None:
        """Map a digest back to an indexed path. Anything else is not served.

        The digest table is rebuilt only when the library replaces its track
        list, so a request costs a dict lookup rather than a scan of the library.
        """
        tracks = self.library.tracks
        if self._token_source is not tracks:
            self._tokens = {token_for(t.path): t.path for t in tracks}
            self._token_source = tracks
        return self._tokens.get(token)

    async def _music(self, request: web.Request) -> web.StreamResponse:
        path = self._resolve(request.match_info["token"].split(".", 1)[0])
        if path is None or not path.is_file():
            raise web.HTTPNotFound()
        ctype = CONTENT_TYPES.get(path.suffix.lower()) \
            or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        # FileResponse implements Range itself, which Sonos relies on to seek.
        return web.FileResponse(path, headers={"Content-Type": ctype,
                                               "Accept-Ranges": "bytes"})

    async def _art(self, request: web.Request) -> web.StreamResponse:
        token = request.match_info["token"]
        cached = self._art_cache.get(token)
        if cached is None:
            path = self._resolve(token)
            if path is None:
                raise web.HTTPNotFound()
            picture = await asyncio.to_thread(tagreader.read_picture, path)
            if picture is None:
                raise web.HTTPNotFound()
            cached = (picture.data, picture.mime)
            if len(self._art_cache) > 512:
                self._art_cache.clear()
            self._art_cache[token] = cached
        data, mime = cached
        return web.Response(body=data, content_type=mime,
                            headers={"Cache-Control": "max-age=86400"})

    async def _clip(self, request: web.Request) -> web.StreamResponse:
        clip = self._clips.get(request.match_info["name"])
        if clip is None:
            raise web.HTTPNotFound()
        log.info("clip %s fetched by %s", request.match_info["name"], request.remote)
        clip.fetched.set()
        return web.Response(body=clip.data, content_type=clip.content_type,
                            headers={"Accept-Ranges": "bytes"})

    async def wait_fetched(self, url: str, timeout: float = 20.0) -> bool:
        """Wait until the speaker has asked for a clip.

        A short-lived caller, such as the CLI, must not stop the server the
        moment it has sent the play command: the speaker fetches the clip a
        moment later, from this process.
        """
        clip = self._clips.get(url.rsplit("/", 1)[-1])
        if clip is None:
            return False
        try:
            await asyncio.wait_for(clip.fetched.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _live(self, request: web.Request) -> web.StreamResponse:
        """Stream this machine's audio output to the speaker, encoded on the fly.

        One encoder per listener: a second speaker asking for the stream gets its
        own process rather than sharing a buffer, which keeps a slow consumer
        from stalling a fast one.
        """
        token = request.query.get("token", "")
        if self._live_token is None or not secrets.compare_digest(
            token.encode(), self._live_token.encode()
        ):
            raise web.HTTPNotFound()
        task = asyncio.current_task()
        self._live_tasks.add(task)
        try:
            return await self._capture(request)
        finally:
            self._live_tasks.discard(task)

    async def _capture(self, request: web.Request) -> web.StreamResponse:
        fmt = request.match_info["fmt"].lower()
        if fmt not in STREAM_FORMATS:
            raise web.HTTPNotFound(text=f"unknown format {fmt!r}")
        args, ctype = STREAM_FORMATS[fmt]
        source = self.capture_source or await default_monitor()
        if not source:
            raise web.HTTPServiceUnavailable(text="no audio monitor source available")

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "pulse", "-i", source,
            "-ac", "2", "-ar", "44100",
            *args, "-",
        ]
        log.info("live capture: %s", " ".join(cmd))
        # Shield process creation so revocation during spawn still reaps it.
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        ))
        response = web.StreamResponse(headers={
            "Content-Type": ctype,
            "Cache-Control": "no-cache, no-store",
            # A live capture has no length and must never be treated as seekable.
            "Accept-Ranges": "none",
        })
        try:
            proc = await asyncio.shield(spawning)
            await response.prepare(request)
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(16384)
                if not chunk:
                    break
                await response.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            log.info("live listener went away")
        finally:
            proc = await spawning
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 3)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            if proc.stderr is not None:
                err = (await proc.stderr.read()).decode("utf-8", "replace").strip()
                if err:
                    log.warning("ffmpeg: %s", err)
        return response


async def default_monitor() -> str | None:
    """The monitor source of the current default sink.

    Capturing a sink's monitor is how the desktop's own output is picked up;
    PipeWire exposes the same names through its PulseAudio compatibility layer,
    so this works on either server.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "pactl", "get-default-sink",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        sink = out.decode().strip()
        return f"{sink}.monitor" if sink else None
    except (OSError, asyncio.CancelledError):
        return None


async def list_monitors() -> list[str]:
    """Every capturable source, so a UI can offer a choice."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pactl", "list", "short", "sources",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return [line.split("\t")[1] for line in out.decode().splitlines() if "\t" in line]
    except OSError:
        return []
