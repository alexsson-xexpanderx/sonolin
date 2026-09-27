"""The websocket client against a fake player speaking the same envelope."""

import asyncio
import json

import pytest
from aiohttp import WSMsgType, web

from nosonpy.ws import API_KEY, SUBPROTOCOL, SonosWebSocket, SonosWebSocketError


async def fake_player(request: web.Request) -> web.WebSocketResponse:
    assert request.headers.get("X-Sonos-Api-Key") == API_KEY
    ws = web.WebSocketResponse(protocols=(SUBPROTOCOL,))
    await ws.prepare(request)
    async for msg in ws:
        if msg.type is not WSMsgType.TEXT:
            continue
        header, body = json.loads(msg.data)
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


def with_player(check):
    async def main():
        app = web.Application()
        app.router.add_get("/websocket/api", fake_player)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
        port = next(iter(site._server.sockets)).getsockname()[1]
        client = SonosWebSocket("127.0.0.1", url=f"ws://127.0.0.1:{port}/websocket/api")
        try:
            await client.connect()
            return await check(client)
        finally:
            await client.close()
            await runner.cleanup()

    return asyncio.run(main())


def test_request_reply_and_target_routing():
    async def check(ws):
        reply = await ws.command("playerVolume", "getVolume", target="RINCON_X")
        assert reply == {"volume": 42, "target": "RINCON_X"}

    with_player(check)


def test_household_is_learned_even_from_a_refusal():
    async def check(ws):
        assert await ws.learn_household() == "Sonos_TEST.loc"

    with_player(check)


def test_refusal_raises_with_the_code():
    async def check(ws):
        with pytest.raises(SonosWebSocketError) as err:
            await ws.command("households", "getHouseholds")
        assert err.value.code == "ERROR_UNSUPPORTED_COMMAND"

    with_player(check)


def test_body_is_passed_through_and_events_are_dispatched():
    async def check(ws):
        seen = []
        ws.on_event(lambda h, b: seen.append((h["type"], b["volume"])))
        assert await ws.command("audioClip", "echo", target="P", name="n", volume=5) == \
            {"name": "n", "volume": 5}
        await asyncio.sleep(0.05)
        assert ("playerVolume", 7) in seen

    with_player(check)


def test_concurrent_commands_do_not_cross():
    async def check(ws):
        replies = await asyncio.gather(*(
            ws.command("audioClip", "echo", target="P", n=i) for i in range(20)
        ))
        assert [r["n"] for r in replies] == list(range(20))

    with_player(check)


def test_timeout_cleans_up():
    async def check(ws):
        with pytest.raises(asyncio.TimeoutError):
            await ws.command("playback", "silence", target="G", timeout=0.2)
        assert ws._waiters == {}

    with_player(check)
