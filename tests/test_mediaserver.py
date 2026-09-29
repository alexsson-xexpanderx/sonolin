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


@pytest.mark.parametrize("stage", ["discovery", "spawn", "stream", "cleanup"])
def test_live_admission_limit(monkeypatch, stage):
    """Held requests share one limit, including while startup is still pending."""
    from unittest.mock import AsyncMock, Mock
    from sonolin import mediaserver

    async def main():
        gate = asyncio.Event()
        full = asyncio.Event()
        calls = 0
        processes = []

        async def block_startup():
            nonlocal calls
            calls += 1
            if calls == mediaserver.MAX_LIVE_ENCODERS:
                full.set()
            await gate.wait()

        async def monitor():
            if stage == "discovery":
                await block_startup()
            return "test.monitor"

        async def communicate():
            if stage == "cleanup":
                await block_startup()
            return b"", b""

        async def spawn(*args, **kwargs):
            if stage == "spawn":
                await block_startup()
            stdout = asyncio.StreamReader()
            proc = Mock(stdout=stdout, returncode=None,
                        communicate=AsyncMock(side_effect=communicate))
            processes.append(proc)
            return proc

        monkeypatch.setattr(mediaserver, "default_monitor", monitor)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        server = MediaServer(host="127.0.0.1")
        await server.start()
        requests = []
        responses = []
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
                formats = list(mediaserver.STREAM_FORMATS)
                requests = [asyncio.create_task(s.get(server.stream_url(formats[i % 3])))
                            for i in range(mediaserver.MAX_LIVE_ENCODERS)]
                if stage in ("stream", "cleanup"):
                    responses = await asyncio.gather(*requests)
                    assert all(r.status == 200 for r in responses)
                    if stage == "cleanup":
                        for proc in processes:
                            proc.stdout.feed_eof()
                        await asyncio.wait_for(full.wait(), 5)
                else:
                    await asyncio.wait_for(full.wait(), 5)
                for method, fmt in [("GET", "flac"), ("GET", "mp3"), ("HEAD", "wav")]:
                    async with s.request(method, server.stream_url(fmt)) as r:
                        assert r.status == 503
                assert len(processes) == (mediaserver.MAX_LIVE_ENCODERS if stage in ("stream", "cleanup") else 0)
                # Saturating live capture must not block other handlers.
                async with s.get(server.base_url) as r:
                    assert r.status == 200
                async with s.get(server.stream_url("aiff")) as r:
                    assert r.status == 404
                gate.set()
                if not responses:
                    responses = await asyncio.gather(*requests)
                for proc in processes:
                    proc.stdout.feed_eof()
                await asyncio.gather(*(r.read() for r in responses))
                # Reap completed encoders before admitting another listener.
                assert all(p.communicate.await_count == 1 for p in processes)
                async with s.get(server.stream_url()) as r:
                    assert r.status == 200
                    processes[-1].stdout.feed_eof()
                    await r.read()
        finally:
            gate.set()
            for proc in processes:
                proc.stdout.feed_eof()
            for response in responses:
                response.close()
            for task in requests:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*requests, return_exceptions=True)
            await server.stop()

    asyncio.run(main())


@pytest.mark.parametrize("failure", ["no_monitor", "spawn", "prepare", "read", "cancel", "timeout"])
def test_live_slot_recovered_after_failure(monkeypatch, failure):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from aiohttp import web
    from sonolin import mediaserver

    async def main():
        server = MediaServer(host="127.0.0.1")
        proc = Mock(returncode=None, stdout=Mock(read=AsyncMock(return_value=b"")),
                    communicate=AsyncMock(return_value=(b"", b"")))
        monitor = AsyncMock(return_value=None if failure == "no_monitor" else "test.monitor")
        spawn = AsyncMock(return_value=proc)
        prepare = AsyncMock()
        monkeypatch.setattr(mediaserver, "default_monitor", monitor)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(web.StreamResponse, "prepare", prepare)
        if failure == "spawn":
            spawn.side_effect = OSError("cannot start encoder")
        elif failure == "prepare":
            prepare.side_effect = ConnectionResetError()
        elif failure == "read":
            proc.stdout.read.side_effect = ConnectionResetError()
        elif failure == "cancel":
            proc.stdout.read.side_effect = asyncio.CancelledError()
        elif failure == "timeout":
            proc.communicate.side_effect = [asyncio.TimeoutError()] + [
                (b"", b"")
            ] * (mediaserver.MAX_LIVE_ENCODERS + 2)

        request = SimpleNamespace(match_info={"fmt": "flac"})
        # Repeated failures must not consume the server's capacity permanently.
        for _ in range(mediaserver.MAX_LIVE_ENCODERS + 1):
            if failure == "no_monitor":
                with pytest.raises(web.HTTPServiceUnavailable) as exc:
                    await server._live(request)
                assert exc.value.text == "no audio monitor source available"
            elif failure == "spawn":
                with pytest.raises(OSError):
                    await server._live(request)
            else:
                await server._live(request)
        assert monitor.await_count == mediaserver.MAX_LIVE_ENCODERS + 1
        if failure not in ("no_monitor", "spawn"):
            assert proc.terminate.call_count == mediaserver.MAX_LIVE_ENCODERS + 1
            assert proc.communicate.await_count >= mediaserver.MAX_LIVE_ENCODERS + 1
        if failure == "timeout":
            assert proc.kill.call_count == 1

    asyncio.run(main())
