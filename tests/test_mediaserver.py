import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest

from sonolin.library import Library
from sonolin import mediaserver
from sonolin.mediaserver import MediaServer, token_for


def serve(music_dir, check):
    """Run `check(server, session)` against a live server on loopback."""

    async def main():
        lib = Library([music_dir]); lib.scan()
        server = MediaServer(lib, host="127.0.0.1")
        await server.start()
        try:
            async with aiohttp.ClientSession() as session:
                return await check(server, lib, session)
        finally:
            await server.stop()

    return asyncio.run(main())


@pytest.mark.parametrize("name,ctype", [
    ("a.flac", "audio/flac"), ("b.mp3", "audio/mpeg"),
    ("c.m4a", "audio/mp4"), ("d.ogg", "audio/ogg"),
])
def test_tracks_are_served_with_the_right_type(music_dir, name, ctype):
    async def check(server, lib, s):
        url = server.music_url(music_dir / name)
        assert url.endswith(Path(name).suffix), "URL must carry the extension"
        async with s.get(url) as r:
            assert r.status == 200
            assert r.headers["Content-Type"] == ctype
            assert await r.read() == (music_dir / name).read_bytes()

    serve(music_dir, check)


def test_byte_ranges_for_seeking(music_dir):
    async def check(server, lib, s):
        url = server.music_url(music_dir / "a.flac")
        async with s.get(url, headers={"Range": "bytes=100-199"}) as r:
            assert r.status == 206
            assert await r.read() == (music_dir / "a.flac").read_bytes()[100:200]

    serve(music_dir, check)


def test_cover_art(music_dir):
    async def check(server, lib, s):
        async with s.get(server.art_url(music_dir / "a.flac")) as r:
            assert r.status == 200 and r.headers["Content-Type"] == "image/png"
        async with s.get(server.art_url(music_dir / "d.ogg")) as r:
            assert r.status == 404  # no art embedded, no cover file

    serve(music_dir, check)


def test_oversized_id3_art_requests(tmp_path):
    path = tmp_path / "oversized.mp3"
    with path.open("wb") as fh:
        fh.write(b"ID3\x03\0\0\x7f\x7f\x7f\x7f")
        fh.truncate(10 + (1 << 28) - 1)

    async def check(server, lib, s):
        assert lib.get(str(path)) is not None
        for _ in range(3):
            async with s.get(server.art_url(path)) as r:
                assert r.status == 404
        (tmp_path / "cover.jpg").write_bytes(b"folder art")
        async with s.get(server.art_url(path)) as r:
            assert r.status == 200
            assert await r.read() == b"folder art"

    serve(tmp_path, check)


def test_only_indexed_files_are_reachable(music_dir):
    async def check(server, lib, s):
        base = server.base_url
        for path in (f"/music/{token_for('/etc/passwd')}",
                     "/music/../../../../etc/passwd",
                     "/music/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
                     f"/art/{token_for('/etc/passwd')}",
                     "/music/", "/clip/nope.wav"):
            async with s.get(base + path) as r:
                assert r.status == 404, path
                assert b"root:" not in await r.read()

    serve(music_dir, check)


def test_clip_publish_and_fetch_notification(music_dir):
    async def check(server, lib, s):
        url = server.add_clip(b"RIFF....WAVE", "audio/wav")
        waiter = asyncio.create_task(server.wait_fetched(url, timeout=5))
        await asyncio.sleep(0.05)
        assert not waiter.done()
        async with s.get(url) as r:
            assert await r.read() == b"RIFF....WAVE"
        assert await waiter is True
        assert await server.wait_fetched(server.base_url + "/clip/unknown.wav", 0.1) is False

    serve(music_dir, check)


def test_clip_urls_are_private_and_independent(tmp_path):
    async def check(server, lib, s):
        data = b"RIFF....WAVE"
        first = server.add_clip(data)
        second = server.add_clip(data)
        assert first != second
        guessed = hashlib.sha1(data[:4096] + str(len(data)).encode()).hexdigest()[:16]
        async with s.get(f"{server.base_url}/clip/{guessed}.wav") as r:
            assert r.status == 404
        for url in (first, first, second):
            assert url.endswith(".wav")
            async with s.get(url) as r:
                assert r.status == 200
                assert r.headers["Content-Type"] == "audio/wav"
                assert r.headers["Cache-Control"] == "no-store"
                assert await r.read() == data
            assert await server.wait_fetched(url, 0.1) is True
            if url == first:
                assert await server.wait_fetched(second, 0.01) is False

    serve(tmp_path, check)


@pytest.mark.parametrize("method", ["get", "head"])
def test_clip_expires_without_another_publication(tmp_path, monkeypatch, method):
    now = 1000.0
    # Replace this module's clock only; asyncio still needs its real clock.
    monkeypatch.setattr(mediaserver, "time", SimpleNamespace(
        monotonic=lambda: now, time=lambda: now))

    async def check(server, lib, s):
        nonlocal now
        url = server.add_clip(b"private speech")
        name = url.rsplit("/", 1)[-1]
        clip = server._clips[name]
        now += 599
        async with s.get(url) as r:
            assert r.status == 200
            assert await r.read() == b"private speech"
        clip.fetched.clear()
        now += 1
        async with getattr(s, method)(url) as r:
            assert r.status == 404
            assert b"private speech" not in await r.read()
        assert not clip.fetched.is_set()
        assert name not in server._clips
        assert await server.wait_fetched(url, 0.01) is False

    serve(tmp_path, check)


def test_publishing_prunes_expired_clips(tmp_path, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(mediaserver, "time", SimpleNamespace(
        monotonic=lambda: now, time=lambda: now))

    async def check(server, lib, s):
        nonlocal now
        old = server.add_clip(b"old")
        now += 600
        fresh = server.add_clip(b"fresh")
        assert old.rsplit("/", 1)[-1] not in server._clips
        async with s.get(fresh) as r:
            assert r.status == 200
            assert await r.read() == b"fresh"

    serve(tmp_path, check)


def test_rescan_is_picked_up(music_dir, tmp_path):
    async def check(server, lib, s):
        extra = tmp_path / "new.flac"
        extra.write_bytes((music_dir / "a.flac").read_bytes())
        async with s.get(server.music_url(extra)) as r:
            assert r.status == 404
        lib.roots.append(tmp_path)
        lib.scan()
        async with s.get(server.music_url(extra)) as r:
            assert r.status == 200

    serve(music_dir, check)


def test_unknown_stream_format(music_dir):
    async def check(server, lib, s):
        async with s.get(server.base_url + "/stream/live.aiff") as r:
            assert r.status == 404

    serve(music_dir, check)


def test_busy_port_falls_back_to_a_free_one(music_dir):
    async def main():
        lib = Library([music_dir]); lib.scan()
        first = MediaServer(lib, host="127.0.0.1", port=0)
        await first.start()
        second = MediaServer(lib, host="127.0.0.1", port=first.port)
        await second.start()
        try:
            assert second.port not in (None, first.port)
        finally:
            await second.stop(); await first.stop()

    asyncio.run(main())
