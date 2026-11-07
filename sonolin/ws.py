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
import hashlib
import itertools
import json
import logging
import socket
import ssl
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .config import Config

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


class NotTrusted(ConnectionError):
    """No usable fingerprint is saved for this speaker. The app asks the user
    whether to trust it; the CLI points at the README."""


def pin_for(pins: object, uid: str, ip: str) -> object:
    """The saved fingerprint for a speaker.

    By the speaker's own id first, which survives the router giving it a new
    address, then by address, as older settings saved it. Choosing the pin by
    id is as safe as by address: whichever device answers must still present
    the certificate the pin names.
    """
    if not isinstance(pins, dict):
        return None
    return (pins.get(uid) if uid else None) or pins.get(ip)


def read_certificate(ip: str, timeout: float = 5.0) -> tuple[str, str]:
    """(SHA-256 fingerprint as ``AA:BB:…``, the certificate's common name).

    Read without checking it, only to show the user, who decides whether to
    trust it; nothing is sent to the speaker beyond the TLS handshake.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((ip, PORT), timeout=timeout) as raw, \
            context.wrap_socket(raw, server_hostname=ip) as tls:
        der = tls.getpeercert(binary_form=True)
    fingerprint = ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())
    return fingerprint, _common_name(der)


def _common_name(der: bytes) -> str:
    """The subject's common name, e.g. a Sonos player's serial number.

    The issuer's name comes first in a certificate and the subject's after
    it, so the last one is the subject's.
    """
    at = der.rfind(b"\x06\x03\x55\x04\x03")  # the object id for "common name"
    if at < 0 or at + 7 > len(der) or der[at + 6] >= 0x80:
        return ""
    length = der[at + 6]
    return der[at + 7:at + 7 + length].decode("utf-8", "replace")


class SonosWebSocket:
    """One long-lived websocket to one player.

    Players use self-signed certificates. An explicitly trusted SHA-256
    certificate fingerprint in Config.websocket_fingerprints authenticates
    each speaker before any commands (including clip URLs) are sent.
    """

    def __init__(
        self, ip: str, *, uid: str = "", session: aiohttp.ClientSession | None = None,
        url: str | None = None,
    ) -> None:
        self.ip = ip
        self.uid = uid
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
        if urlsplit(self.url).scheme != "wss":
            raise ConnectionError("Speaker connections require wss:// (TLS)")
        pin = pin_for(Config.load().websocket_fingerprints, self.uid, self.ip)
        try:
            fingerprint = aiohttp.Fingerprint(bytes.fromhex(pin.replace(":", "")))
        except (AttributeError, TypeError, ValueError) as exc:
            raise NotTrusted(
                f"The speaker at {self.ip} is not trusted for announcements yet. "
                "Trust it from Sonolin's Announce page, or see README.md."
            ) from exc
        if self._session is None:
            self._session = aiohttp.ClientSession()
        ws = None
        try:
            ws = await self._session.ws_connect(
                self.url,
                headers={"X-Sonos-Api-Key": API_KEY},
                protocols=(SUBPROTOCOL,),
                ssl=fingerprint,
                heartbeat=30,
            )
            # Check the final transport too: redirects can downgrade to plaintext,
            # for which aiohttp's fingerprint check is a no-op.
            peer = ws.get_extra_info("ssl_object")
            if peer is None or hashlib.sha256(peer.getpeercert(binary_form=True)).digest() \
                    != fingerprint.fingerprint:
                raise ConnectionError(f"Speaker certificate verification failed for {self.ip}")
        except BaseException:
            if ws is not None:
                await ws.close()
            if self._owns_session:
                await self._session.close()
                self._session = None
            raise
        self._ws = ws
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
