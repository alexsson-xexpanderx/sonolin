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


@pytest.mark.parametrize("replace_after_scan", [False, True])
def test_file_symlinks_are_not_served(tmp_path, monkeypatch, replace_after_scan):
    music = tmp_path / "music"
    music.mkdir()
    secret = tmp_path / "private.txt"
    secret.write_bytes(b"private data outside the music folder")
    track = music / "song.mp3"
    if replace_after_scan:
        track.write_bytes(b"local track")
    else:
        track.symlink_to(secret)

    def unexpected_picture_read(path):
        pytest.fail("rejected paths must not reach the artwork reader")

    monkeypatch.setattr("sonolin.mediaserver.tagreader.read_picture", unexpected_picture_read)

    async def check(server, lib, s):
        if replace_after_scan:
            # Populate the token cache before replacing the indexed file.
            async with s.get(server.music_url(track)) as r:
                assert r.status == 200
                assert await r.read() == b"local track"
            track.unlink()
            track.symlink_to(secret)
        for url in (server.music_url(track), server.art_url(track)):
            async with s.get(url) as r:
                assert r.status == 404
                assert secret.read_bytes() not in await r.read()

    serve(music, check)


def test_explicitly_added_file_outside_roots_is_served(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    track = tmp_path / "one-off.mp3"
    track.write_bytes(b"explicitly selected track")
    link = tmp_path / "selected.mp3"
    link.symlink_to(track)

    async def check(server, lib, s):
        added = lib.add_files([link])
        assert [t.path for t in added] == [track]
        async with s.get(server.music_url(added[0].path)) as r:
            assert r.status == 200
            assert await r.read() == track.read_bytes()

    serve(music, check)


def test_parent_symlink_after_scan_is_not_served(tmp_path):
    music = tmp_path / "music"
    album = music / "album"
    album.mkdir(parents=True)
    track = album / "song.mp3"
    track.write_bytes(b"local track")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / track.name).write_bytes(b"private audio")
    (outside / "cover.jpg").write_bytes(b"private art")

    async def check(server, lib, s):
        album.rename(music / "old-album")
        album.symlink_to(outside, target_is_directory=True)
        for url in (server.music_url(track), server.art_url(track)):
            async with s.get(url) as r:
                assert r.status == 404
                assert b"private" not in await r.read()

    serve(music, check)


@pytest.mark.parametrize("when", ["before_open", "after_open"])
def test_file_swap_during_response(tmp_path, monkeypatch, when):
    track = tmp_path / "song.mp3"
    track.write_bytes(b"local track")
    secret = tmp_path / "private.txt"
    secret.write_bytes(b"private target")
    cls = mediaserver._LocalFileResponse if when == "before_open" else mediaserver.web.FileResponse
    original_prepare = cls.prepare

    async def swap(response, request):
        track.unlink()
        track.symlink_to(secret)
        return await original_prepare(response, request)

    monkeypatch.setattr(cls, "prepare", swap)

    async def check(server, lib, s):
        async with s.get(server.music_url(track)) as r:
            assert r.status == (404 if when == "before_open" else 200)
            body = await r.read()
            assert b"private target" not in body
            if when == "after_open":
                assert body == b"local track"

    serve(tmp_path, check)


def test_folder_art_symlink_is_not_served(tmp_path):
    track = tmp_path / "song.mp3"
    track.write_bytes(b"local track")
    secret = tmp_path / "private.txt"
    secret.write_bytes(b"private target")
    (tmp_path / "cover.jpg").symlink_to(secret)

    async def check(server, lib, s):
        async with s.get(server.art_url(track)) as r:
            assert r.status == 404
            assert secret.read_bytes() not in await r.read()

    serve(tmp_path, check)


def test_head_and_conditional_music_requests(music_dir):
    async def check(server, lib, s):
        path = music_dir / "a.flac"
        url = server.music_url(path)
        async with s.head(url) as r:
            assert r.status == 200
            assert int(r.headers["Content-Length"]) == path.stat().st_size
            assert await r.read() == b""
            etag = r.headers["ETag"]
        async with s.get(url, headers={"If-None-Match": etag}) as r:
            assert r.status == 304
            assert await r.read() == b""
        async with s.get(url, headers={"Range": "bytes=-10"}) as r:
            assert r.status == 206
            assert await r.read() == path.read_bytes()[-10:]
        async with s.get(url, headers={"Range": "bytes=999999999-"}) as r:
            assert r.status == 416

    serve(music_dir, check)


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
def test_response_keeps_descriptor_until_reader_finishes(tmp_path, monkeypatch, outcome):
    track = tmp_path / "song.mp3"
    track.write_bytes(b"local track")

    async def check():
        entered = asyncio.Event()
        finish = asyncio.Event()
        pinned = None

        async def prepare(response, request):
            nonlocal pinned
            pinned = response._path
            entered.set()
            await finish.wait()
            assert pinned.read_bytes() == b"local track"
            if outcome == "error":
                raise RuntimeError("reader failed")

        monkeypatch.setattr(mediaserver.web.FileResponse, "prepare", prepare)
        response = mediaserver._LocalFileResponse(track)
        task = asyncio.create_task(response.prepare(None))
        await asyncio.wait_for(entered.wait(), 5)
        if outcome == "cancel":
            task.cancel()
            await asyncio.sleep(0)
            assert pinned.read_bytes() == b"local track"
        finish.set()
        if outcome == "success":
            await task
        else:
            error = asyncio.CancelledError if outcome == "cancel" else RuntimeError
            with pytest.raises(error):
                await task
        assert not pinned.exists(), "the descriptor must be closed on every exit"

    asyncio.run(check())


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
        await server.start_stream()
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

        server.port = 1  # URL construction only; this test invokes the handler directly.
        await server.start_stream()
        request = SimpleNamespace(match_info={"fmt": "flac"},
                                  query={"token": server._live_token})
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


@pytest.fixture
def live_encoder(monkeypatch):
    from unittest.mock import AsyncMock, Mock

    processes = []

    async def spawn(*args, **kwargs):
        proc = Mock(returncode=None, stdout=asyncio.StreamReader(),
                    communicate=AsyncMock(return_value=(b"", b"")))
        processes.append(proc)
        return proc

    monitor = AsyncMock(return_value="synthetic.monitor")
    monkeypatch.setattr(mediaserver, "default_monitor", monitor)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return processes, monitor


@pytest.mark.parametrize("method", ["get", "head"])
def test_live_requires_active_capability(tmp_path, live_encoder, monkeypatch, method):
    processes, monitor = live_encoder
    now = 1000.0
    monkeypatch.setattr(mediaserver, "time", SimpleNamespace(monotonic=lambda: now))

    async def check(server, lib, s):
        nonlocal now

        async def denied(url):
            async with getattr(s, method)(url) as response:
                assert response.status == 404

        public = server.base_url + "/stream/live.flac"
        await denied(public)
        first = await server.start_stream()
        await denied(public)
        await denied(public + "?token=guessed")
        async with s.get(server.base_url) as response:
            assert server._live_token not in await response.text()
        second = await server.start_stream()
        assert first != second
        await denied(first)
        now += mediaserver.LIVE_IDLE_TTL
        await denied(second)
        third = await server.start_stream()
        await server.stop_stream()
        await denied(third)
        assert not processes
        monitor.assert_not_awaited()

    serve(tmp_path, check)


@pytest.mark.parametrize("ending", ["stop", "replace", "shutdown"])
def test_live_revocation_closes_connected_listener(tmp_path, live_encoder, ending):
    processes, _ = live_encoder

    async def check(server, lib, s):
        url = await server.start_stream()
        response = await s.get(url)
        assert response.status == 200
        processes[0].stdout.feed_data(b"synthetic audio")
        assert await response.content.readexactly(15) == b"synthetic audio"
        if ending == "stop":
            await server.stop_stream()
        elif ending == "replace":
            assert await server.start_stream() != url
        else:
            await server.stop()
            await server.start()
            url = server.base_url + "/stream/" + url.split("/stream/", 1)[1]
        assert await asyncio.wait_for(response.read(), 5) == b""
        processes[0].terminate.assert_called_once()
        processes[0].communicate.assert_awaited_once()
        async with s.get(url) as denied:
            assert denied.status == 404
        assert len(processes) == 1

    serve(tmp_path, check)


def test_live_activity_renews_capability_then_expires(tmp_path, live_encoder, monkeypatch):
    processes, _ = live_encoder
    now = 1000.0
    monkeypatch.setattr(mediaserver, "time", SimpleNamespace(monotonic=lambda: now))

    async def check(server, lib, s):
        nonlocal now
        url = await server.start_stream()
        async with s.get(url) as response:
            now += mediaserver.LIVE_IDLE_TTL + 1
            processes[0].stdout.feed_data(b"audio")
            assert await response.content.readexactly(5) == b"audio"
            # A long running stream still permits speaker reconnects.
            async with s.get(url) as retry:
                assert retry.status == 200
                processes[1].stdout.feed_eof()
                await retry.read()
            processes[0].stdout.feed_eof()
            await response.read()
        now += mediaserver.LIVE_IDLE_TTL
        async with s.get(url) as denied:
            assert denied.status == 404
        assert len(processes) == 2

    serve(tmp_path, check)


@pytest.mark.parametrize("stage", ["discovery", "spawn", "cleanup"])
def test_live_revocation_during_startup_or_cleanup(tmp_path, monkeypatch, stage):
    from unittest.mock import AsyncMock, Mock

    async def check(server, lib, s):
        entered = asyncio.Event()
        release = asyncio.Event()
        proc = Mock(returncode=None, stdout=asyncio.StreamReader())
        proc.stdout.feed_eof()

        async def monitor():
            if stage == "discovery":
                entered.set()
                await release.wait()
            return "synthetic.monitor"

        async def spawn(*args, **kwargs):
            if stage == "spawn":
                entered.set()
                await release.wait()
            return proc

        async def communicate():
            if stage == "cleanup":
                entered.set()
                await release.wait()
            return b"", b""

        proc.communicate = AsyncMock(side_effect=communicate)
        monkeypatch.setattr(mediaserver, "default_monitor", monitor)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        url = await server.start_stream()
        request = asyncio.create_task(s.get(url))
        await asyncio.wait_for(entered.wait(), 5)
        stopping = asyncio.create_task(server.stop_stream())
        await asyncio.sleep(0)
        async with s.get(url) as denied:
            assert denied.status == 404
        release.set()
        await asyncio.wait_for(stopping, 5)
        result = await asyncio.gather(request, return_exceptions=True)
        if isinstance(result[0], aiohttp.ClientResponse):
            result[0].close()
        if stage == "discovery":
            proc.terminate.assert_not_called()
        else:
            proc.terminate.assert_called_once()
            proc.communicate.assert_awaited_once()
        assert not server._live_slots.locked()

    serve(tmp_path, check)


def test_stop_during_session_replacement_keeps_capture_disabled(tmp_path, live_encoder):
    from unittest.mock import AsyncMock

    processes, _ = live_encoder

    async def check(server, lib, s):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def cleanup():
            entered.set()
            await release.wait()
            return b"", b""

        url = await server.start_stream()
        response = await s.get(url)
        processes[0].communicate = AsyncMock(side_effect=cleanup)
        replacing = asyncio.create_task(server.start_stream())
        await asyncio.wait_for(entered.wait(), 5)
        stopping = asyncio.create_task(server.stop_stream())
        await asyncio.sleep(0)
        release.set()
        replacement, _ = await asyncio.wait_for(asyncio.gather(replacing, stopping), 5)
        assert server._live_token is None
        async with s.get(replacement) as denied:
            assert denied.status == 404
        await response.read()
        processes[0].communicate.assert_awaited_once()

    serve(tmp_path, check)
