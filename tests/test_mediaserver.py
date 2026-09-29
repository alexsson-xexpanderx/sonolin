import asyncio
from pathlib import Path

import aiohttp
import pytest

from sonolin.library import Library
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


@pytest.mark.parametrize("fmt", ["flac", "mp3", "wav"])
def test_live_requires_current_session(tmp_path, monkeypatch, fmt):
    from unittest.mock import AsyncMock

    monitor = AsyncMock(return_value=None)
    spawn = AsyncMock()
    monkeypatch.setattr("sonolin.mediaserver.default_monitor", monitor)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def check(server, lib, s):
        public = f"{server.base_url}/stream/live.{fmt}"
        async def denied(url):
            for method in (s.get, s.head):
                async with method(url) as r:
                    assert r.status == 404

        await denied(public)
        url = await server.start_stream(fmt)
        await denied(public)
        await denied(public + "?token=wrong")
        await denied(public + "?token=é")
        monitor.assert_not_called()
        spawn.assert_not_called()
        # Valid authorization reaches monitor discovery (no monitor here).
        async with s.get(url) as r:
            assert r.status == 503
        assert monitor.await_count == 1
        replacement = await server.start_stream(fmt)
        assert replacement != url
        await denied(url)
        await server.stop_stream()
        await denied(replacement)
        assert monitor.await_count == 1
        spawn.assert_not_called()

    serve(tmp_path, check)


@pytest.mark.parametrize("end", ["stop", "replace", "shutdown"])
def test_revocation_ends_active_capture(tmp_path, monkeypatch, end):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    async def check(server, lib, s):
        stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
        stdout.feed_data(b"desktop audio")
        stderr.feed_eof()
        proc = SimpleNamespace(stdout=stdout, stderr=stderr, returncode=None,
                               terminate=Mock(), kill=Mock(), wait=AsyncMock(return_value=0))
        spawn = AsyncMock(return_value=proc)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        server.capture_source = "test.monitor"
        url = await server.start_stream()
        async with s.get(url) as r:
            assert r.status == 200
            assert await r.content.readexactly(13) == b"desktop audio"
            if end == "shutdown":
                await asyncio.wait_for(server.stop(), 2)
                await server.start()
            elif end == "replace":
                await asyncio.wait_for(server.start_stream(), 2)
            else:
                await asyncio.wait_for(server.stop_stream(), 2)
            assert await asyncio.wait_for(r.read(), 2) == b""
        proc.terminate.assert_called_once()
        proc.wait.assert_awaited_once()
        assert not server._live_tasks
        # Test the stale capability against the current port after a restart.
        async with s.get(server.base_url + "/" + url.split("/", 3)[3]) as r:
            assert r.status == 404
        spawn.assert_awaited_once()

    serve(tmp_path, check)


@pytest.mark.parametrize("phase", ["monitor", "spawn", "prepare"])
def test_revocation_cleans_up_capture_startup(tmp_path, monkeypatch, phase):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from aiohttp.test_utils import make_mocked_request

    async def check(server, lib, s):
        reached, release = asyncio.Event(), asyncio.Event()
        stderr = asyncio.StreamReader()
        stderr.feed_eof()
        proc = SimpleNamespace(stderr=stderr, returncode=None,
                               terminate=Mock(), kill=Mock(), wait=AsyncMock(return_value=0))

        async def pause(result):
            reached.set()
            await release.wait()
            return result

        async def source():
            return await pause("test.monitor") if phase == "monitor" else "test.monitor"
        async def spawn(*args, **kwargs):
            return await pause(proc) if phase == "spawn" else proc
        async def prepare(*args):
            return await pause(None)
        monkeypatch.setattr("sonolin.mediaserver.default_monitor", source)
        spawn_mock = AsyncMock(side_effect=spawn)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_mock)
        monkeypatch.setattr("aiohttp.web.StreamResponse.prepare", prepare)
        url = await server.start_stream()
        request = make_mocked_request("GET", "/" + url.split("/", 3)[3],
                                      match_info={"fmt": "flac"})
        listener = asyncio.create_task(server._live(request))
        await asyncio.wait_for(reached.wait(), 2)
        stopping = asyncio.create_task(server.stop_stream())
        await asyncio.sleep(0)  # let revocation cancel the listener
        release.set()
        await asyncio.wait_for(stopping, 2)
        assert listener.done()
        if phase == "monitor":
            spawn_mock.assert_not_called()
        else:
            proc.terminate.assert_called_once()
            proc.wait.assert_awaited_once()
        assert not server._live_tasks

    serve(tmp_path, check)
