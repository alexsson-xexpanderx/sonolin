"""Local Sonos WebSocket API client (port 1443).

This is the modern "Sonos Control API" spoken directly to a player over TLS on
the LAN, with no cloud round trip and no developer registration. It is the tier
libnoson never implemented and that SoCo still does not cover, and it is the
only way to reach audio clips (announcements that duck the music instead of
stopping it), home-theater options and volume ducking.

The transport is a plain JSON WebSocket. Every message is a two element array,
``[header, body]``. Requests carry a namespace, a command and exactly one
target id; responses echo the command name in ``header["response"]`` and carry
``header["success"]``. Unsolicited events arrive in the same shape with
``header["type"]`` set and no ``response`` key.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import ssl
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

# Published in every open-source client (Home Assistant's sonos-websocket among
# them). Players accept it for local control; it identifies the protocol, not a
# user, and carries no entitlement.
API_KEY = "123e4567-e89b-12d3-a456-426655440000"
SUBPROTOCOL = "v1.api.smartspeaker.audio"
PORT = 1443

#: Namespace -> the id the command is addressed to. "self" namespaces go to the
#: player itself, "coordinator" namespaces must go to the group coordinator.
TARGET = {
    "audioClip": "playerId",
    "playerVolume": "playerId",
    "homeTheater": "playerId",
    "settings": "playerId",
    "playback": "groupId",
    "playbackMetadata": "groupId",
    "groupVolume": "groupId",
    "playlists": "householdId",
    "favorites": "householdId",
    "groups": "householdId",
    "households": "householdId",
}


class SonosWebSocketError(RuntimeError):
    """A command was rejected by the player."""

    def __init__(self, command: str, code: str, reason: str) -> None:
        super().__init__(f"{command}: {code}: {reason}")
        self.command = command
        self.code = code
        self.reason = reason


class SonosWebSocket:
    """One long-lived websocket to one player.

    Players present a self-signed certificate for a name that does not resolve,
    so the certificate is not verified. The connection is still encrypted; it is
    LAN-local and carries no credentials beyond the public API key above.
    """

    def __init__(
        self, ip: str, *, session: aiohttp.ClientSession | None = None, url: str | None = None
    ) -> None:
        self.ip = ip
        self.url = url or f"wss://{ip}:{PORT}/websocket/api"
        self.household_id: str | None = None
        self._session = session
        self._owns_session = session is None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._waiters: dict[str, asyncio.Future] = {}
        self._listeners: list[Callable[[dict, dict], Awaitable[None] | None]] = []
        self._pump: asyncio.Task | None = None

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        if self._ws is not None and not self._ws.closed:
            return
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        if self._session is None:
            self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(
            self.url,
            headers={"X-Sonos-Api-Key": API_KEY},
            protocols=(SUBPROTOCOL,),
            ssl=ctx,
            heartbeat=30,
        )
        self._pump = asyncio.create_task(self._read_loop())
        log.debug("connected to %s", self.url)

    async def close(self) -> None:
        if self._pump:
            self._pump.cancel()
            self._pump = None
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._owns_session and self._session:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> SonosWebSocket:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- plumbing ----------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    header, body = json.loads(msg.data)
                except (ValueError, TypeError):
                    log.warning("unparseable frame from %s: %r", self.ip, msg.data[:200])
                    continue
                # Every frame carries the household id; it is the only way to
                # learn it locally, and it differs from the UPnP household id.
                if not self.household_id and header.get("householdId"):
                    self.household_id = header["householdId"]
                waiter = self._waiters.pop(header.get("cmdId", ""), None)
                if waiter is not None and not waiter.done():
                    waiter.set_result((header, body))
                else:
                    await self._dispatch_event(header, body)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("read loop for %s stopped", self.ip)

    async def _dispatch_event(self, header: dict, body: dict) -> None:
        for fn in list(self._listeners):
            try:
                result = fn(header, body)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                log.exception("event listener failed")

    def on_event(self, fn: Callable[[dict, dict], Awaitable[None] | None]) -> None:
        """Register a callback for unsolicited frames (subscription events)."""
        self._listeners.append(fn)

    async def command(
        self,
        namespace: str,
        command: str,
        *,
        target: str | None = None,
        version: int = 1,
        timeout: float = 10.0,
        **body: Any,
    ) -> dict:
        """Send one command and return its response body.

        `target` is the player id, group id or household id as the namespace
        requires; `TARGET` says which. Raises `SonosWebSocketError` if the
        player rejects the command.
        """
        await self.connect()
        assert self._ws is not None
        cmd_id = str(next(self._ids))
        header: dict[str, Any] = {
            "namespace": f"{namespace}:{version}",
            "command": command,
            "cmdId": cmd_id,
        }
        key = TARGET.get(namespace)
        if key and target:
            header[key] = target
        elif key == "householdId" and self.household_id:
            header["householdId"] = self.household_id

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters[cmd_id] = fut
        await self._ws.send_str(json.dumps([header, body]))
        try:
            reply_header, reply_body = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._waiters.pop(cmd_id, None)
            raise
        if reply_header.get("success") is False:
            raise SonosWebSocketError(
                command,
                reply_body.get("errorCode", "UNKNOWN"),
                reply_body.get("reason", ""),
            )
        return reply_body

    async def learn_household(self) -> str | None:
        """Discover the household id this socket belongs to.

        The websocket household id carries a location suffix that the UPnP one
        does not, so it cannot be borrowed from SoCo. Any command reveals it in
        the response header, including one the player refuses.
        """
        if self.household_id:
            return self.household_id
        try:
            await self.command("households", "getHouseholds")
        except (SonosWebSocketError, asyncio.TimeoutError):
            pass  # the header we wanted arrived regardless
        return self.household_id
