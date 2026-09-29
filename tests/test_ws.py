"""The websocket client against a fake player speaking the same envelope."""

import asyncio
import json
import hashlib
import ssl
import subprocess
from contextlib import asynccontextmanager

import pytest
from aiohttp import ClientSession, ServerFingerprintMismatch, WSMsgType, web

from sonolin.config import Config

from sonolin.ws import API_KEY, SUBPROTOCOL, SonosWebSocket, SonosWebSocketError


FRAMES = web.AppKey("frames", list)


async def fake_player(request: web.Request) -> web.WebSocketResponse:
    assert request.headers.get("X-Sonos-Api-Key") == API_KEY
    ws = web.WebSocketResponse(protocols=(SUBPROTOCOL,))
    await ws.prepare(request)
    async for msg in ws:
        if msg.type is not WSMsgType.TEXT:
            continue
        header, body = json.loads(msg.data)
        request.app[FRAMES].append((header, body))
        base = {"namespace": header["namespace"], "response": header["command"],
                "cmdId": header["cmdId"], "householdId": "Sonos_TEST.loc"}
        # An unsolicited event first, to prove replies are matched by cmdId.
        await ws.send_str(json.dumps([{"namespace": "playerVolume:1", "type": "playerVolume",
                                       "householdId": "Sonos_TEST.loc"}, {"volume": 7}]))
        cmd = header["command"]
        if cmd == "getVolume":
            await ws.send_str(json.dumps([{**base, "success": True},
                                          {"volume": 42, "target": header.get("playerId")}]))
        elif cmd == "getHouseholds":
            await ws.send_str(json.dumps([{**base, "success": False},
                                          {"errorCode": "ERROR_UNSUPPORTED_COMMAND",
                                           "reason": "not here"}]))
        elif cmd == "echo":
            await ws.send_str(json.dumps([{**base, "success": True}, body]))
        # "silence" never answers
    return ws


@pytest.fixture(scope="session")
def certificates(tmp_path_factory):
    root = tmp_path_factory.mktemp("certificates")
    result = []
    for n in range(2):
        cert, key = root / f"{n}.pem", root / f"{n}.key"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "1",
            "-subj", "/CN=speaker.invalid",
        ], check=True, capture_output=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
        result.append((context, pin))
    return result


@pytest.fixture
def trusted_speaker(certificates):
    context, pin = certificates[0]
    Config(websocket_fingerprints={"127.0.0.1": pin}).save()
    return context


@asynccontextmanager
async def player_server(context, handler=fake_player):
    app = web.Application()
    app[FRAMES] = []
    app.router.add_get("/websocket/api", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
        await site.start()
        port = next(iter(site._server.sockets)).getsockname()[1]
        scheme = "wss" if context else "ws"
        yield f"{scheme}://127.0.0.1:{port}/websocket/api", app[FRAMES]
    finally:
        await runner.cleanup()


def with_player(check, context):
    async def main():
        async with player_server(context) as (url, _):
            async with SonosWebSocket("127.0.0.1", url=url) as client:
                return await check(client)

    return asyncio.run(main())


def test_request_reply_and_target_routing(trusted_speaker):
    async def check(ws):
        reply = await ws.command("playerVolume", "getVolume", target="RINCON_X")
        assert reply == {"volume": 42, "target": "RINCON_X"}

    with_player(check, trusted_speaker)


def test_household_is_learned_even_from_a_refusal(trusted_speaker):
    async def check(ws):
        assert await ws.learn_household() == "Sonos_TEST.loc"

    with_player(check, trusted_speaker)


def test_refusal_raises_with_the_code(trusted_speaker):
    async def check(ws):
        with pytest.raises(SonosWebSocketError) as err:
            await ws.command("households", "getHouseholds")
        assert err.value.code == "ERROR_UNSUPPORTED_COMMAND"

    with_player(check, trusted_speaker)


def test_body_is_passed_through_and_events_are_dispatched(trusted_speaker):
    async def check(ws):
        seen = []
        ws.on_event(lambda h, b: seen.append((h["type"], b["volume"])))
        assert await ws.command("audioClip", "echo", target="P", name="n", volume=5) == \
            {"name": "n", "volume": 5}
        await asyncio.sleep(0.05)
        assert ("playerVolume", 7) in seen

    with_player(check, trusted_speaker)


def test_concurrent_commands_do_not_cross(trusted_speaker):
    async def check(ws):
        replies = await asyncio.gather(*(
            ws.command("audioClip", "echo", target="P", n=i) for i in range(20)
        ))
        assert [r["n"] for r in replies] == list(range(20))

    with_player(check, trusted_speaker)


def test_timeout_cleans_up(trusted_speaker):
    async def check(ws):
        with pytest.raises(asyncio.TimeoutError):
            await ws.command("playback", "silence", target="G", timeout=0.2)
        assert ws._waiters == {}

    with_player(check, trusted_speaker)


@pytest.mark.parametrize("pin", [None, "", "bad", "00" * 20, 42])
def test_missing_or_invalid_pin_fails_before_connecting(pin):
    Config(websocket_fingerprints={"127.0.0.1": pin}).save()

    async def check():
        client = SonosWebSocket("127.0.0.1")
        with pytest.raises(ConnectionError, match="verified SHA-256"):
            await client.command("audioClip", "loadAudioClip", streamUrl="http://private/clip")
        assert client._session is None
        assert client._ws is None
        assert client._waiters == {}

    asyncio.run(check())


def test_pin_for_another_speaker_is_not_used(certificates):
    Config(websocket_fingerprints={"127.0.0.2": certificates[0][1]}).save()

    async def check():
        with pytest.raises(ConnectionError, match="verified SHA-256"):
            await SonosWebSocket("127.0.0.1").connect()

    asyncio.run(check())


def test_plaintext_url_is_rejected(trusted_speaker):
    async def check():
        async with player_server(None) as (url, frames):
            with pytest.raises(ConnectionError, match="require wss"):
                await SonosWebSocket("127.0.0.1", url=url).command(
                    "audioClip", "loadAudioClip", streamUrl="http://private/clip")
            assert frames == []

    asyncio.run(check())


def test_impostor_never_receives_clip_url(trusted_speaker, certificates):
    async def check():
        async with player_server(certificates[1][0]) as (url, frames):
            client = SonosWebSocket("127.0.0.1", url=url)
            with pytest.raises(ServerFingerprintMismatch):
                await client.command("audioClip", "loadAudioClip", streamUrl="http://private/clip")
            assert frames == []
            assert client._ws is None
            assert client._session is None
            assert client._pump is None

    asyncio.run(check())


def test_reconnect_checks_identity_again(trusted_speaker, certificates):
    async def check():
        async with player_server(trusted_speaker) as (url, _):
            client = SonosWebSocket("127.0.0.1", url=url)
            await client.command("audioClip", "echo", streamUrl="http://private/clip")
            await client.close()
        async with player_server(certificates[1][0]) as (url, frames):
            client.url = url
            with pytest.raises(ServerFingerprintMismatch):
                await client.command("audioClip", "loadAudioClip", streamUrl="http://private/clip")
            assert frames == []

    asyncio.run(check())


@pytest.mark.parametrize("tls", [False, True])
def test_redirect_cannot_bypass_pin(trusted_speaker, certificates, tls):
    async def check():
        context = certificates[1][0] if tls else None
        async with player_server(context) as (destination, frames):
            async def redirect(request):
                raise web.HTTPFound(destination.replace("wss:", "https:").replace("ws:", "http:"))

            async with player_server(trusted_speaker, redirect) as (url, _):
                client = SonosWebSocket("127.0.0.1", url=url)
                error = ServerFingerprintMismatch if tls else ConnectionError
                with pytest.raises(error):
                    await client.command("audioClip", "loadAudioClip", streamUrl="http://private/clip")
                assert frames == []
                assert client._ws is None
                assert client._session is None

    asyncio.run(check())


def test_caller_session_still_requires_pin(trusted_speaker, certificates):
    async def check():
        async with ClientSession() as session:
            async with player_server(certificates[1][0]) as (url, frames):
                client = SonosWebSocket("127.0.0.1", url=url, session=session)
                with pytest.raises(ServerFingerprintMismatch):
                    await client.command("audioClip", "loadAudioClip", streamUrl="http://private/clip")
                assert frames == []
                assert not session.closed

    asyncio.run(check())
